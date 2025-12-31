from __future__ import annotations
import json
import asyncio
from typing import Any, Dict, Optional

from app.database.connection import get_db_context
from app.models.schemas import (
    TripStartIn, TripEndIn, TelemetryIn, AlertIn
)
from app.repositories.devices_repo import upsert_device, update_last_seen
from app.repositories.trips_repo import create_trip, close_trip, get_active_trip_for_device
from app.repositories.telemetry_repo import insert_trip_data
from app.repositories.alerts_repo import insert_alert
from app.repositories.trips_repo import get_trip_by_id
from app.repositories.predictions_repo import insert_prediction
from app.ml.predictor import predict_crash
from app.services.connection_manager import manager
from app.services.risk_assessor import RiskAssessor

from collections import deque
from enum import Enum
import time

class CrashState(Enum):
    NORMAL = "normal"
    INVESTIGATING = "investigating"

# Configuration
RING_BUFFER_SIZE = 30
POST_WINDOW_SIZE = 20
COOLDOWN_SECONDS = 10.0

# In-memory state per trip_id
# Structure: trip_id -> {
#   "state": CrashState,
#   "ring_buffer": deque(maxlen=RING_BUFFER_SIZE),
#   "post_buffer": list,  # collects samples during investigating
#   "pre_window": list,   # snapshot of ring buffer at trigger
#   "last_trigger_ts": float 
# }
_TRIP_STATE: Dict[str, Dict[str, Any]] = {}



# Single in-process queue for persistence work
_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=10_000)

# ------------------------------------------------------------------------------
# NOTE: This worker relies on in-memory state (_TRIP_STATE, _RISK_STATE).
# It MUST run in a SINGLE PROCESS (uvicorn workers=1) to work correctly.
# If multiple workers are used, state will be fragmented and features will break.
# ------------------------------------------------------------------------------

# Active trip map (device_id -> trip_id). In-memory; persist later if needed.
_ACTIVE_TRIP: dict[str, str] = {}

# In-memory risk state per trip_id
# Structure: trip_id -> {
#   "ring_buffer": deque(maxlen=20),
#   "last_gps": {"lat": float, "lng": float, "ts": str} or None,
#   "last_sent_ts": float,
#   "user_id": str or None
# }
_RISK_STATE: Dict[str, Dict[str, Any]] = {}


async def enqueue_persist(msg: Dict[str, Any]) -> None:
    """
    Put a validated message onto the persistence queue.
    Call this from your /ws/ingest handler after schema validation.
    """
    await _QUEUE.put(msg)


async def start_persist_worker() -> None:
    """
    Run forever, consuming messages and writing them to the DB.
    Call this once at app startup (create a background task).
    """
    while True:
        msg = await _QUEUE.get()
        try:
            await _handle_message(msg)
        except Exception as e:
            # In production, log this with structured logs
            # so a bad message doesn't crash the loop.
            print(f"[persist] error: {e}")
        finally:
            _QUEUE.task_done()


# -----------------------
# Message dispatch
# -----------------------

async def _handle_message(msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "trip_start":
        await _handle_trip_start(TripStartIn(**msg))
    elif mtype == "telemetry":
        await _handle_telemetry(TelemetryIn(**msg))
    elif mtype == "trip_end":
        await _handle_trip_end(TripEndIn(**msg))
    else:
        # Unknown message type; ignore or log
        pass


# -----------------------
# Handlers
# -----------------------

async def _handle_trip_start(payload: TripStartIn) -> None:
    """
    Create a Trip and remember it for the device (active trip).
    """
    async with get_db_context() as db:
        # Ensure device exists and get it to find owner
        device = await upsert_device(db, payload.device_id)

        # 1. Check if there is an existing active trip for this device
        existing_trip = await get_active_trip_for_device(db, payload.device_id)
        if existing_trip:
            # Auto-close the previous trip
            print(f"Auto-closing dangling trip {existing_trip.trip_id} for device {payload.device_id}")
            
            # Try to find last known location for end_lat/end_lng
            from app.repositories.trips_repo import TripsRepo
            last_loc = await TripsRepo.get_last_known_location(db, existing_trip.trip_id)
            
            end_lat = last_loc.lat if last_loc else None
            end_lng = last_loc.lng if last_loc else None

            await close_trip(
                db=db,
                trip_id=existing_trip.trip_id,
                end_time=payload.ts, # Use new trip start time as end time
                end_lat=end_lat,
                end_lng=end_lng,
                crash_detected=None
            )
            # Cleanup state for the closed trip
            _TRIP_STATE.pop(existing_trip.trip_id, None)
            _RISK_STATE.pop(existing_trip.trip_id, None)

        if not device.user_id:
            print(f"[persist] WARNING: Creating trip for device {payload.device_id} with NO USER linked!")

        # Create trip (linked to device owner)
        trip = await create_trip(
            db=db,
            user_id=device.user_id,
            device_id=payload.device_id,
            start_time=payload.ts,
            # start_time=payload.ts,
        )
        # Map device -> active trip
        _ACTIVE_TRIP[payload.device_id] = trip.trip_id
        await db.commit()


async def _resolve_active_trip_id(device_id: str) -> Optional[str]:
    tid = _ACTIVE_TRIP.get(device_id)
    if tid:
        async with get_db_context() as db:
            trip = await get_trip_by_id(db, tid)
            if trip and trip.status == "recording":
                return tid
        # stale or cancelled/completed -> drop it
        _ACTIVE_TRIP.pop(device_id, None)

    async with get_db_context() as db:
        trip = await get_active_trip_for_device(db, device_id)
        return trip.trip_id if trip else None


async def _handle_telemetry(payload: TelemetryIn) -> None:
    """
    Save one telemetry sample (TripData).
    If there is no active trip for this device, auto-create one.
    """
    async with get_db_context() as db:
        # Ensure device row and heartbeat
        device = await upsert_device(db, payload.device_id)
        await update_last_seen(db, payload.device_id, payload.ts)

        # Try to resolve trip_id: payload may send it, else use active map/DB
        trip_id = payload.trip_id or await _resolve_active_trip_id(payload.device_id)

        # If still no trip_id, auto-create a new trip
        if not trip_id:
            if not device.user_id:
                print(f"[persist] WARNING: Auto-creating trip for device {payload.device_id} with NO USER linked!")
            trip = await create_trip(
                db=db,
                user_id=device.user_id,          # may be None if device not linked yet
                device_id=payload.device_id,
                start_time=payload.ts,
            )
            trip_id = trip.trip_id
            _ACTIVE_TRIP[payload.device_id] = trip_id
            # no commit yet, we’ll commit after inserting trip_data

        v_kmh = None
        if payload.velocity and payload.velocity.kmh is not None:
             v_kmh = payload.velocity.kmh

        # Flatten blocks for DB insert
        await insert_trip_data(
            db,
            device_id=payload.device_id,
            timestamp=payload.ts,
            trip_id=trip_id,
            lat=payload.gps.lat,
            lng=payload.gps.lng,
            speed_kmh=v_kmh,
            acc_x=payload.imu.ax, acc_y=payload.imu.ay, acc_z=payload.imu.az,
            gyro_x=payload.imu.gx, gyro_y=payload.imu.gy, gyro_z=payload.imu.gz,
            heart_rate=payload.heart_rate.hr,
            crash_flag=payload.crash_flag,
        )
        await db.commit()

        # -----------------------------
        # DRIVING RISK PIPELINE
        # -----------------------------
        if trip_id not in _RISK_STATE:
            _RISK_STATE[trip_id] = {
                "ring_buffer": deque(maxlen=20),
                "last_gps": None,
                "last_sent_ts": 0.0,
                "user_id": None
            }
        
        risk_state = _RISK_STATE[trip_id]
        
        # Cache user_id if missing (try device owner logic from before)
        # We know device object might have user_id if we fetched it, 
        # but 'device' variable scope above is closed? No, it's in async with block?
        # Actually 'device' is available inside the block above (lines 164-200).
        # But we are outside the block?
        # Wait, the indentation suggests we are inside `async with get_db_context() as db:` block?
        # Line 200 `await db.commit()` is indented.
        # My replacement is AFTER line 200. I should match indentation.
        
        # Populate user_id cache if needed
        if not risk_state["user_id"] and device.user_id:
             risk_state["user_id"] = device.user_id
             
        # Add to ring buffer
        risk_msg = payload.model_dump()
        risk_state["ring_buffer"].append(risk_msg)
        
        now_ts = time.time()
        
        # Throttle: 1 sec
        if (now_ts - risk_state["last_sent_ts"]) >= 1.0:
            # Assess Risk
            assessment = RiskAssessor.assess_risk(
                list(risk_state["ring_buffer"]),
                risk_state["last_gps"]
            )
            
            # Broadcast if we have a user
            uid = risk_state["user_id"]
            if not uid:
                # Last ditch effort: try resolving from DB if we really need to?
                # or just skip. We really need user_id to broadcast.
                # If 'device.user_id' was None, maybe trip has it?
                # We can try to query trip if we are still in DB session?
                # Yes, we are.
                if not uid:
                     t_row = await get_trip_by_id(db, trip_id)
                     if t_row and t_row.user_id:
                         uid = t_row.user_id
                         risk_state["user_id"] = uid
            
            if uid:
                conns = len(manager.user_connections.get(uid, []))
                print(f"[Risk] Broadcast to {uid} (Connections: {conns}) Level={assessment['level']}")
                
                # Make payload JSON-safe (handle datetimes/enums if any)
                safe_payload = json.loads(json.dumps(assessment, default=str))
                
                await manager.broadcast_to_user(uid, {
                    "type": "RISK_STATUS",
                    "payload": safe_payload
                })
            
            # Update state
            risk_state["last_sent_ts"] = now_ts
            
            # Update last_gps if current msg has valid GPS
            if payload.gps and payload.gps.ok and payload.gps.lat:
                risk_state["last_gps"] = {
                    "lat": payload.gps.lat,
                    "lng": payload.gps.lng,
                    "ts": payload.ts # store original TS for Haversine delta
                }

        # -----------------------------
        # CRASH DETECTION PIPELINE
        # -----------------------------
        # 1. Init state if needed
        if trip_id not in _TRIP_STATE:
            _TRIP_STATE[trip_id] = {
                "state": CrashState.NORMAL,
                "ring_buffer": deque(maxlen=RING_BUFFER_SIZE),
                "post_buffer": [],
                "pre_window": [],
                "last_trigger_ts": 0.0
            }
        
        trip_state = _TRIP_STATE[trip_id]
        
        # Convert payload to strict dict for buffering/prediction
        # We need to preserve the structure expected by predictor.py
        msg_dict = payload.model_dump()
        
        # 2. Update Ring Buffer (always, in NORMAL state)
        # In INVESTIGATING, we might arguably pause ring buffer updates or continue?
        # Usually we continue so we don't lose data if we reset.
        trip_state["ring_buffer"].append(msg_dict)
        
        current_ts = time.time()
        
        # 3. State Machine
        if trip_state["state"] == CrashState.NORMAL:
            # Check trigger
            if payload.crash_flag:
                # Check cooldown
                if (current_ts - trip_state["last_trigger_ts"]) > COOLDOWN_SECONDS:
                    print(f"[Crash] Triggered for trip {trip_id}. Investigating...")
                    trip_state["state"] = CrashState.INVESTIGATING
                    trip_state["last_trigger_ts"] = current_ts
                    
                    # Snapshot pre-window
                    # Issue 1 Fix: Exclude current msg_dict from pre_window to avoid duplicate
                    trip_state["pre_window"] = list(trip_state["ring_buffer"])[:-1]
                    # Start post-buffer with CURRENT message
                    trip_state["post_buffer"] = [msg_dict]
                else:
                    print(f"[Crash] Cooldown active for trip {trip_id}. Ignoring trigger.")
                    
        elif trip_state["state"] == CrashState.INVESTIGATING:
            # Collect post-window samples
            trip_state["post_buffer"].append(msg_dict)
            
            if len(trip_state["post_buffer"]) >= POST_WINDOW_SIZE:
                # Ready to predict
                full_window = trip_state["pre_window"] + trip_state["post_buffer"]
                
                print(f"[Crash] Running inference on {len(full_window)} samples...")
                
                # Run Fake ML
                result = predict_crash(full_window)
                # result = { "label": "crash"/"no_crash", "prob": float, "model": ... }
                
                # Persist Prediction
                pred_label = result.get("label", "unknown")
                pred_prob = result.get("prob", 0.0)
                model_name = result.get("model", "unknown")
                
                # We need a fresh DB session or reuse existing? 
                # The existing 'db' context is closed/committed above.
                # We should open a new one or move commit to end. 
                # actually 'db' scope ended at line 170? 
                # Wait, line 136 `async with get_db_context() as db:` covers this entire block?
                # The indentation in the replacement must match.
                # My replacement effectively is OUTSIDE the `async with` block if I unindent?
                # No, I should keep it separate or reuse.
                # Since line 170 `await db.commit()` is the end of the block in original file.
                # I will create a new transaction for the prediction/alert to ensure atomicity 
                # and avoid "session closed" errors if I reuse `db` after commit.
                
                async with get_db_context() as pred_db:
                    await insert_prediction(
                        pred_db,
                        device_id=payload.device_id,
                        trip_id=trip_id,
                        model_name=model_name,
                        label=pred_label,
                        score=pred_prob,
                        ts=payload.ts,
                        meta_json={"window_size": len(full_window)}
                    )
                    
                    if pred_label == "crash":
                        print(f"[Crash] CONFIRMED crash (prob={pred_prob:.2f})!")

                        # Optimization: get user_id from arguments if possible, but payload doesn't have it.
                        # We can fetch it.
                        trip_row = await get_trip_by_id(pred_db, trip_id)
                        user_id = trip_row.user_id if trip_row else None
                        
                        # 1. Create Alert in DB
                        alert = await insert_alert(
                            pred_db,
                            device_id=payload.device_id,
                            trip_id=trip_id,
                            ts=payload.ts,
                            alert_type="crash_server",
                            severity="critical",
                            message=f"Crash detected with {pred_prob:.0%} confidence.",
                            user_id=user_id,
                            payload_json=result
                        )
                        
                        # 2. Broadcast via Websocket
                        if user_id:
                            await manager.broadcast_to_user(user_id, {
                                "type": "ALERT_CRITICAL",
                                "payload": {
                                    "alert_id": alert.alert_id,
                                    "message": alert.message,
                                    "device_id": payload.device_id,
                                    "ts": payload.ts.isoformat()
                                }
                            })

                    await pred_db.commit()
                
                # Reset State
                trip_state["state"] = CrashState.NORMAL
                trip_state["post_buffer"] = []
                trip_state["pre_window"] = []


async def _handle_trip_end(payload: TripEndIn) -> None:
    """
    Close the current trip (if any) for the device.
    DB is the source of truth.
    """
    trip_id = await _resolve_active_trip_id(payload.device_id)
    if not trip_id:
        return  # Nothing to close

    async with get_db_context() as db:
        await close_trip(
            db=db,
            trip_id=trip_id,
            end_time=payload.ts,
            crash_detected=None,
        )
        await db.commit()

    # Clear runtime state and active trip cache
    _TRIP_STATE.pop(trip_id, None)
    _RISK_STATE.pop(trip_id, None)
    _ACTIVE_TRIP.pop(payload.device_id, None)


async def _handle_alert(payload: AlertIn) -> None:
    """
    Save an alert (from device ML or elsewhere).
    """
    async with get_db_context() as db:
        # Attach trip if missing
        trip_id = payload.trip_id or await _resolve_active_trip_id(payload.device_id)

        await insert_alert(
            db,
            device_id=payload.device_id,
            ts=payload.ts,
            alert_type=payload.alert_type.value,
            severity=payload.severity.value,
            message=payload.message,
            user_id=None,          # fill from auth later
            trip_id=trip_id,
            payload_json=payload.payload,
        )
        await db.commit()



# | Method                               | What it does                                                          | Why we need it                                                                             |
# | ------------------------------------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
# | `enqueue_persist(msg)`               | Puts a validated message onto the queue.                              | Decouples WebSocket ingest from DB work; keeps ingest fast and safe.                       |
# | `start_persist_worker()`             | Infinite loop that consumes messages from the queue and writes to DB. | Centralized, reliable persistence; isolates failures to one message, not the whole server. |
# | `_handle_message(msg)`               | Dispatches by `type` to the correct handler.                          | Clean separation of behaviors: start/end trip vs telemetry vs alert.                       |
# | `_handle_trip_start(payload)`        | Creates a `Trip` row and updates the active trip map.                 | Starts session context; later used to attach telemetry to the right trip.                  |
# | `_resolve_active_trip_id(device_id)` | Returns current trip ID (from memory, else DB).                       | Ensures telemetry attaches even if `trip_id` isn’t sent every message.                     |
# | `_handle_telemetry(payload)`         | Upserts device, updates `last_seen_at`, inserts one `TripData` row.   | The core storage path for your time-series data.                                           |
# | `_handle_trip_end(payload)`          | Closes the active trip and removes it from the active map.            | Marks the ride as finished; keeps active map consistent.                                   |
# | `_handle_alert(payload)`             | Inserts an `Alert` row (edge or server ML), attaching trip if known.  | Persists risk/crash events for dashboards and history.                                     |



# | Table        | What it stores                                                         | Source                                  |
# | ------------ | ---------------------------------------------------------------------- | --------------------------------------- |
# | **Devices**  | Each helmet (device_id, last_seen_at, etc.)                            | Every telemetry/trip_start updates it   |
# | **Trips**    | Each ride session (start/end, stats)                                   | From `trip_start` / `trip_end` messages |
# | **TripData** | All raw sensor telemetry (heart rate, accel, gyro, gps, battery, etc.) | From every `telemetry` message          |
# | **Alerts**   | Immediate alerts coming **from the Raspberry Pi ML** (edge detection)  | From `alert` messages                   |
