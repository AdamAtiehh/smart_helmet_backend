from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from enum import Enum
from typing import Any, Dict, Optional

from app.database.connection import get_db_context
from app.ml.predictor import predict_crash
from app.models.schemas import AlertIn, TelemetryIn, TripEndIn, TripStartIn
from app.repositories.alerts_repo import insert_alert
from app.repositories.devices_repo import update_last_seen, upsert_device
from app.repositories.predictions_repo import insert_prediction
from app.repositories.telemetry_repo import insert_trip_data
from app.repositories.trips_repo import close_trip, create_trip, get_active_trip_for_device, get_trip_by_id
from app.services.connection_manager import manager
from app.services.risk_assessor import RiskAssessor

# ======================================================================================
# WORKER OVERVIEW (COMMENTED TABLE)
# ======================================================================================
# | Function / Method                     | What it does                                                   | Used by |
# |--------------------------------------|----------------------------------------------------------------|--------|
# | enqueue_persist(msg)                 | Pushes a validated message into the persistence queue          | ws/ingest |
# | start_persist_worker()               | Infinite loop: consumes queue and calls _handle_message()      | startup |
# | _handle_message(msg)                 | Dispatches by msg["type"] to the proper handler                | worker |
# | _handle_trip_start(payload)          | Closes dangling trip (if any), creates a new trip, sets active | worker |
# | _resolve_active_trip_id(device_id)   | Gets active trip_id from memory, falls back to DB              | worker |
# | _handle_telemetry(payload)           | Inserts TripData + runs Risk + Crash pipelines                 | worker |
# | _handle_trip_end(payload)            | Closes active trip, clears in-memory state                     | worker |
# | _handle_alert(payload)               | Inserts Alert row (edge/device or upstream alerts)             | worker |
#
# ======================================================================================
# DB TABLES (COMMENTED TABLE)
# ======================================================================================
# | Table        | What it stores                                                        | Source                        |
# |-------------|-----------------------------------------------------------------------|-------------------------------|
# | devices     | Each helmet (device_id, last_seen_at, etc.)                           | trip_start + telemetry        |
# | trips       | Each ride/session (start/end, computed stats, status)                 | trip_start / trip_end         |
# | trip_data   | Time-series telemetry (acc/gyro/gps/hr/speed/crash_flag, etc.)         | telemetry                     |
# | alerts      | Alerts (edge ML or server ML)                                         | alert messages + server crash |
# | predictions | Server-side ML inference results                                      | crash pipeline                |

# ======================================================================================
# IMPORTANT NOTE
# ======================================================================================
# This worker relies on in-memory state (_ACTIVE_TRIP, _TRIP_STATE, _RISK_STATE).
# It MUST run in a SINGLE PROCESS (uvicorn --workers 1).
# Multiple workers will fragment state and break behavior.
# ======================================================================================


# -----------------------------
# Crash pipeline config/state
# -----------------------------
class CrashState(Enum):
    NORMAL = "normal"
    INVESTIGATING = "investigating"


RING_BUFFER_SIZE = 30
POST_WINDOW_SIZE = 20
COOLDOWN_SECONDS = 10.0

# trip_id -> crash state
_TRIP_STATE: Dict[str, Dict[str, Any]] = {}

# device_id -> trip_id (active recording trip)
_ACTIVE_TRIP: Dict[str, str] = {}

# trip_id -> risk state
_RISK_STATE: Dict[str, Dict[str, Any]] = {}

# Single in-process queue for persistence work
_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=10_000)


# ======================================================================================
# Public API
# ======================================================================================
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
            print(f"[persist] error: {e}")
        finally:
            _QUEUE.task_done()


# ======================================================================================
# Dispatcher
# ======================================================================================
async def _handle_message(msg: dict) -> None:
    mtype = msg.get("type")

    if mtype == "trip_start":
        await _handle_trip_start(TripStartIn(**msg))
    elif mtype == "telemetry":
        await _handle_telemetry(TelemetryIn(**msg))
    elif mtype == "trip_end":
        await _handle_trip_end(TripEndIn(**msg))
    elif mtype == "alert":
        # ✅ Critical fix: you had _handle_alert(), but never dispatched "alert"
        await _handle_alert(AlertIn(**msg))
    else:
        # Unknown message type; ignore or log
        pass


# ======================================================================================
# Trip helpers
# ======================================================================================
async def _resolve_active_trip_id(device_id: str) -> Optional[str]:
    """
    Prefer the in-memory map (fast). If it looks stale, fall back to DB.
    """
    tid = _ACTIVE_TRIP.get(device_id)
    if tid:
        async with get_db_context() as db:
            trip = await get_trip_by_id(db, tid)
            if trip and trip.status == "recording":
                return tid
        # stale -> drop
        _ACTIVE_TRIP.pop(device_id, None)

    async with get_db_context() as db:
        trip = await get_active_trip_for_device(db, device_id)
        return trip.trip_id if trip else None


# ======================================================================================
# Handlers
# ======================================================================================
async def _handle_trip_start(payload: TripStartIn) -> None:
    """
    Create a Trip and remember it for the device (active trip).
    If a dangling trip exists, auto-close it first.
    """
    async with get_db_context() as db:
        device = await upsert_device(db, payload.device_id)

        existing_trip = await get_active_trip_for_device(db, payload.device_id)
        if existing_trip:
            print(
                f"Auto-closing dangling trip {existing_trip.trip_id} "
                f"for device {payload.device_id}"
            )

            # Get last known end location (if supported)
            from app.repositories.trips_repo import TripsRepo  # local import avoids cycles

            last_loc = await TripsRepo.get_last_known_location(db, existing_trip.trip_id)
            end_lat = last_loc.lat if last_loc else None
            end_lng = last_loc.lng if last_loc else None

            await close_trip(
                db=db,
                trip_id=existing_trip.trip_id,
                end_time=payload.ts,
                end_lat=end_lat,
                end_lng=end_lng,
                crash_detected=None,
            )

            # cleanup runtime state
            _TRIP_STATE.pop(existing_trip.trip_id, None)
            _RISK_STATE.pop(existing_trip.trip_id, None)

        if not device.user_id:
            print(
                f"[persist] WARNING: Creating trip for device {payload.device_id} "
                f"with NO USER linked!"
            )

        trip = await create_trip(
            db=db,
            user_id=device.user_id,
            device_id=payload.device_id,
            start_time=payload.ts,
        )

        _ACTIVE_TRIP[payload.device_id] = trip.trip_id
        await db.commit()


async def _handle_telemetry(payload: TelemetryIn) -> None:
    """
    Save one telemetry sample (TripData).
    If there is no active trip for this device, auto-create one.
    Also runs:
      - driving risk assessment broadcast
      - crash inference pipeline
    """
    async with get_db_context() as db:
        # Ensure device exists + update last_seen
        device = await upsert_device(db, payload.device_id)
        await update_last_seen(db, payload.device_id, payload.ts)

        # Resolve trip_id: prefer payload, else memory/DB
        trip_id = payload.trip_id or await _resolve_active_trip_id(payload.device_id)

        # Auto-create a trip if missing
        if not trip_id:
            if not device.user_id:
                print(
                    f"[persist] WARNING: Auto-creating trip for device {payload.device_id} "
                    f"with NO USER linked!"
                )

            trip = await create_trip(
                db=db,
                user_id=device.user_id,
                device_id=payload.device_id,
                start_time=payload.ts,
            )
            trip_id = trip.trip_id
            _ACTIVE_TRIP[payload.device_id] = trip_id

        # velocity.kmh -> speed_kmh column
        v_kmh = payload.velocity.kmh if payload.velocity and payload.velocity.kmh is not None else None

        # Insert TripData row
        await insert_trip_data(
            db,
            device_id=payload.device_id,
            timestamp=payload.ts,
            trip_id=trip_id,
            lat=payload.gps.lat,
            lng=payload.gps.lng,
            speed_kmh=v_kmh,
            acc_x=payload.imu.ax,
            acc_y=payload.imu.ay,
            acc_z=payload.imu.az,
            gyro_x=payload.imu.gx,
            gyro_y=payload.imu.gy,
            gyro_z=payload.imu.gz,
            heart_rate=payload.heart_rate.hr,
            crash_flag=payload.crash_flag,
        )

        await db.commit()

        # --------------------------------------------------
        # DRIVING RISK PIPELINE
        # --------------------------------------------------
        if trip_id not in _RISK_STATE:
            _RISK_STATE[trip_id] = {
                "ring_buffer": deque(maxlen=20),
                "last_gps": None,
                "last_sent_ts": 0.0,
                "user_id": None,
            }

        risk_state = _RISK_STATE[trip_id]

        # cache user_id when possible
        if not risk_state["user_id"] and device.user_id:
            risk_state["user_id"] = device.user_id

        risk_state["ring_buffer"].append(payload.model_dump())

        now_ts = time.time()

        # throttle: 1 broadcast/sec
        if (now_ts - risk_state["last_sent_ts"]) >= 1.0:
            assessment = RiskAssessor.assess_risk(
                list(risk_state["ring_buffer"]),
                risk_state["last_gps"],
            )

            uid = risk_state["user_id"]

            # fallback: try from trip row if user_id missing
            if not uid:
                t_row = await get_trip_by_id(db, trip_id)
                if t_row and t_row.user_id:
                    uid = t_row.user_id
                    risk_state["user_id"] = uid

            if uid:
                conns = len(manager.user_connections.get(uid, []))
                print(
                    f"[Risk] Broadcast to {uid} (Connections: {conns}) "
                    f"Level={assessment['level']}"
                )

                safe_payload = json.loads(json.dumps(assessment, default=str))
                await manager.broadcast_to_user(
                    uid,
                    {"type": "RISK_STATUS", "payload": safe_payload},
                )

            risk_state["last_sent_ts"] = now_ts

            # update last_gps (used for speed fallback)
            if payload.gps and payload.gps.ok and payload.gps.lat:
                risk_state["last_gps"] = {
                    "lat": payload.gps.lat,
                    "lng": payload.gps.lng,
                    "ts": payload.ts,
                }

        # --------------------------------------------------
        # CRASH DETECTION PIPELINE
        # --------------------------------------------------
        if trip_id not in _TRIP_STATE:
            _TRIP_STATE[trip_id] = {
                "state": CrashState.NORMAL,
                "ring_buffer": deque(maxlen=RING_BUFFER_SIZE),
                "post_buffer": [],
                "pre_window": [],
                "last_trigger_ts": 0.0,
            }

        trip_state = _TRIP_STATE[trip_id]
        msg_dict = payload.model_dump()
        trip_state["ring_buffer"].append(msg_dict)

        current_ts = time.time()

        if trip_state["state"] == CrashState.NORMAL:
            if payload.crash_flag:
                if (current_ts - trip_state["last_trigger_ts"]) > COOLDOWN_SECONDS:
                    print(f"[Crash] Triggered for trip {trip_id}. Investigating...")
                    trip_state["state"] = CrashState.INVESTIGATING
                    trip_state["last_trigger_ts"] = current_ts

                    # pre window excludes current msg (avoid duplication)
                    trip_state["pre_window"] = list(trip_state["ring_buffer"])[:-1]
                    trip_state["post_buffer"] = [msg_dict]
                else:
                    print(f"[Crash] Cooldown active for trip {trip_id}. Ignoring trigger.")

        elif trip_state["state"] == CrashState.INVESTIGATING:
            trip_state["post_buffer"].append(msg_dict)

            if len(trip_state["post_buffer"]) >= POST_WINDOW_SIZE:
                full_window = trip_state["pre_window"] + trip_state["post_buffer"]
                print(f"[Crash] Running inference on {len(full_window)} samples...")

                result = predict_crash(full_window)

                pred_label = result.get("label", "unknown")
                pred_prob = float(result.get("prob", 0.0) or 0.0)
                model_name = result.get("model", "unknown")

                async with get_db_context() as pred_db:
                    await insert_prediction(
                        pred_db,
                        device_id=payload.device_id,
                        trip_id=trip_id,
                        model_name=model_name,
                        label=pred_label,
                        score=pred_prob,
                        ts=payload.ts,
                        meta_json={"window_size": len(full_window)},
                    )

                    if pred_label == "crash":
                        print(f"[Crash] CONFIRMED crash (prob={pred_prob:.2f})!")

                        trip_row = await get_trip_by_id(pred_db, trip_id)
                        user_id = trip_row.user_id if trip_row else None

                        alert = await insert_alert(
                            pred_db,
                            device_id=payload.device_id,
                            trip_id=trip_id,
                            ts=payload.ts,
                            alert_type="crash_server",
                            severity="critical",
                            message=f"Crash detected with {pred_prob:.0%} confidence.",
                            user_id=user_id,
                            payload_json=result,
                        )

                        if user_id:
                            await manager.broadcast_to_user(
                                user_id,
                                {
                                    "type": "ALERT_CRITICAL",
                                    "payload": {
                                        "alert_id": alert.alert_id,
                                        "message": alert.message,
                                        "device_id": payload.device_id,
                                        "ts": payload.ts.isoformat(),
                                    },
                                },
                            )

                    await pred_db.commit()

                # reset crash state
                trip_state["state"] = CrashState.NORMAL
                trip_state["post_buffer"] = []
                trip_state["pre_window"] = []


async def _handle_trip_end(payload: TripEndIn) -> None:
    """
    Close the current trip (if any) for the device.
    """
    trip_id = await _resolve_active_trip_id(payload.device_id)
    if not trip_id:
        return

    async with get_db_context() as db:
        await close_trip(
            db=db,
            trip_id=trip_id,
            end_time=payload.ts,
            crash_detected=None,
        )
        await db.commit()

    _TRIP_STATE.pop(trip_id, None)
    _RISK_STATE.pop(trip_id, None)
    _ACTIVE_TRIP.pop(payload.device_id, None)


async def _handle_alert(payload: AlertIn) -> None:
    """
    Save an alert (from device ML or upstream).
    """
    async with get_db_context() as db:
        trip_id = payload.trip_id or await _resolve_active_trip_id(payload.device_id)

        await insert_alert(
            db,
            device_id=payload.device_id,
            ts=payload.ts,
            alert_type=payload.alert_type.value,
            severity=payload.severity.value,
            message=payload.message,
            user_id=None,  # fill from auth later if needed
            trip_id=trip_id,
            payload_json=payload.payload,
        )
        await db.commit()
