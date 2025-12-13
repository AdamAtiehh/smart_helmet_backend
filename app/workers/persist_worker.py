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



# Single in-process queue for persistence work
_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=10_000)

# Active trip map (device_id -> trip_id). In-memory; persist later if needed.
_ACTIVE_TRIP: dict[str, str] = {}


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
            # no commit yet, we’ll commit after inserting trip_data

        # Flatten blocks for DB insert
        await insert_trip_data(
            db,
            device_id=payload.device_id,
            timestamp=payload.ts,
            trip_id=trip_id,
            lat=payload.gps.lat,
            lng=payload.gps.lng,
            acc_x=payload.imu.ax, acc_y=payload.imu.ay, acc_z=payload.imu.az,
            gyro_x=payload.imu.gx, gyro_y=payload.imu.gy, gyro_z=payload.imu.gz,
            heart_rate=payload.heart_rate.hr,
            crash_flag=payload.crash_flag,
        )
        await db.commit()


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

    # Optional: keep cache clean if you still store it elsewhere
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
