from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from enum import Enum
from typing import Any, Dict, Optional, List

import numpy as np

from app.database.connection import get_db_context
from app.ml.predict_crash import predict_crash
from app.models.schemas import AlertIn, TelemetryIn, TripEndIn, TripStartIn,InferenceState
from app.repositories.alerts_repo import insert_alert
from app.repositories.devices_repo import update_last_seen, upsert_device
from app.repositories.predictions_repo import insert_prediction
from app.repositories.telemetry_repo import insert_trip_data
from app.repositories.trips_repo import close_trip, create_trip, get_active_trip_for_device, get_trip_by_id
from app.services.connection_manager import manager
from app.services.risk_assessor import RiskAssessor

# ======================================================================================
# WORKER OVERVIEW
# ======================================================================================
# The persist worker has been refactored to support CONTINUOUS UNSUPERVISED INFERENCE.
# It no longer relies on a device-sent "crash_flag".
#
# Logic:
# 1. Maintain a ring buffer of the last WINDOW_SECONDS of telemetry.
# 2. On every message, append to buffer and trim old messages by timestamp.
# 3. If buffer covers enough time (MIN_SAMPLES), run IFOREST inference.
# 4. If score < threshold (anomaly), increment "anomaly_streak".
# 5. Helper checks: 
#    - Warmup: Don't alert in first WARMUP_WINDOWS.
#    - Motion Evidence: accel/gyro max must exceed adaptive thresholds (percentile of recent normal).
#    - Cooldown: Don't alert again within COOLDOWN_SECONDS.
# 6. If all pass -> CRASH ALERT.

# -----------------------------
# Configuration Constants
# -----------------------------
WINDOW_SECONDS = 20
WINDOW_SAMPLES = 10     # feed last 10 samples into model (matches training)
MIN_SAMPLES = 10
NORMAL_HISTORY_MAX = 300
COOLDOWN_SECONDS = 45.0

# Tunable via Env Vars for easier testing
WARMUP_WINDOWS = int(os.getenv("WARMUP_WINDOWS", "10"))
STREAK_MIN = int(os.getenv("STREAK_MIN", "2"))
EVIDENCE_PERCENTILE = float(os.getenv("EVIDENCE_PERCENTILE", "95.0"))

print(f"[PersistWorker] Config: WARMUP={WARMUP_WINDOWS} STREAK={STREAK_MIN} EVIDENCE_PCT={EVIDENCE_PERCENTILE}")


# Single in-process queue for persistence work
_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=10_000)

# device_id -> trip_id (active recording trip)
_ACTIVE_TRIP: Dict[str, str] = {}

# trip_id -> risk state
_RISK_STATE: Dict[str, Dict[str, Any]] = {}

# -----------------------------
# Inference State
# -----------------------------
class InferenceState:
    def __init__(self):
        self.ring_buffer = deque()  # stores dicts
        self.anomaly_streak = 0
        self.last_alert_ts = 0.0
        self.warmup_counter = 0
        
        # Adaptive threshold history (circular buffers for max values of NORMAL windows)
        # We'll store just the max acc/gyro of each normal window to compute percentiles
        self.normal_acc_max_history = deque(maxlen=NORMAL_HISTORY_MAX)
        self.normal_gyro_max_history = deque(maxlen=NORMAL_HISTORY_MAX)

_INFERENCE_STATE: Dict[str, InferenceState] = {}


# ======================================================================================
# Public API
# ======================================================================================
async def enqueue_persist(msg: Dict[str, Any]) -> None:
    """
    Put a validated message onto the persistence queue.
    """
    await _QUEUE.put(msg)


async def start_persist_worker() -> None:
    """
    Run forever, consuming messages and writing them to the DB.
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
        await _handle_alert(AlertIn(**msg))
    else:
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
    Persist telemetry + broadcast risk status + run crash ML only in "event mode".

    NOTE (required InferenceState fields):
      - ring_buffer: deque
      - event_until_ts: float
      - last_infer_ts: float
      - last_gate_ts: float
      - warmup_counter: int
      - anomaly_streak: int
      - last_alert_ts: float
      - normal_acc_max_history: deque/list
      - normal_gyro_max_history: deque/list
    """

    async with get_db_context() as db:
        # -----------------------------
        # 1) Upsert device + last seen
        # -----------------------------
        device = await upsert_device(db, payload.device_id)
        await update_last_seen(db, payload.device_id, payload.ts)

        # -----------------------------
        # 2) Resolve / create trip
        # -----------------------------
        trip_id = payload.trip_id or await _resolve_active_trip_id(payload.device_id)

        if not trip_id:
            trip = await create_trip(
                db=db,
                user_id=device.user_id,
                device_id=payload.device_id,
                start_time=payload.ts,
            )
            trip_id = trip.trip_id
            _ACTIVE_TRIP[payload.device_id] = trip_id

        # -----------------------------
        # 3) Insert TripData
        # -----------------------------
        v_kmh = payload.velocity.kmh if (payload.velocity and payload.velocity.kmh is not None) else None

        lat = payload.gps.lat if payload.gps else None
        lng = payload.gps.lng if payload.gps else None

        ax = payload.imu.ax if payload.imu else None
        ay = payload.imu.ay if payload.imu else None
        az = payload.imu.az if payload.imu else None
        gx = payload.imu.gx if payload.imu else None
        gy = payload.imu.gy if payload.imu else None
        gz = payload.imu.gz if payload.imu else None

        hr = payload.heart_rate.hr if payload.heart_rate else None

        await insert_trip_data(
            db,
            device_id=payload.device_id,
            timestamp=payload.ts,
            trip_id=trip_id,
            lat=lat,
            lng=lng,
            speed_kmh=v_kmh,
            acc_x=ax,
            acc_y=ay,
            acc_z=az,
            gyro_x=gx,
            gyro_y=gy,
            gyro_z=gz,
            heart_rate=hr,
            crash_flag=False,  # Force False
        )
        await db.commit()

    # --------------------------------------------------
    # 4) Ensure inference state exists early (so risk can gate ML)
    # --------------------------------------------------
    if trip_id not in _INFERENCE_STATE:
        _INFERENCE_STATE[trip_id] = InferenceState()
    inf_state = _INFERENCE_STATE[trip_id]

  # --------------------------------------------------
# 5) DRIVING RISK PIPELINE (fixed routing)
# --------------------------------------------------

    device_id = payload.device_id  # should exist for telemetry
    if not device_id:
        return  # or just skip risk

    # State per DEVICE (not trip). Prevents cross-user mixing.
    if device_id not in _RISK_STATE:
        _RISK_STATE[device_id] = {
            "ring_buffer": deque(maxlen=20),
            "last_gps": None,
            "last_sent_ts": 0.0,
            "user_id": None,
        }

    risk_st = _RISK_STATE[device_id]

    # ✅ Always refresh the owner user_id (don’t keep stale one)
    risk_st["user_id"] = device.user_id

    # Append message for risk assessor
    risk_st["ring_buffer"].append(payload.model_dump())
    now_sys = time.time()

    if (now_sys - risk_st["last_sent_ts"]) >= 1.0:
        assessment = RiskAssessor.assess_risk(
            list(risk_st["ring_buffer"]),
            risk_st["last_gps"],
        )

        uid = risk_st["user_id"]
        if uid:
            await manager.broadcast_to_user(
                uid,
                {"type": "RISK_STATUS", "payload": assessment},
            )

        # --- ML gating hook ---
        if assessment.get("ml_gate"):
            last_gate = getattr(inf_state, "last_gate_ts", 0.0)
            if (now_sys - last_gate) > 2.0:
                inf_state.event_until_ts = max(getattr(inf_state, "event_until_ts", 0.0), now_sys + 12.0)
                inf_state.last_gate_ts = now_sys

        risk_st["last_sent_ts"] = now_sys

    # Update last GPS if valid
    if payload.gps and payload.gps.ok and payload.gps.lat:
        risk_st["last_gps"] = {"lat": payload.gps.lat, "lng": payload.gps.lng, "ts": payload.ts}

        # --------------------------------------------------
    # 6) CRASH DETECTION PIPELINE (event-mode + throttled)
    # --------------------------------------------------
    # Always update inference buffer (so you keep context),
    # but ONLY run inference during event mode.
    msg = {
        "_ts_epoch": payload.ts.timestamp(),

        "imu": {
            "ok": bool(payload.imu.ok) if payload.imu else False,
            "sleep": bool(payload.imu.sleep) if payload.imu else False,
            "ax": float(payload.imu.ax) if payload.imu and payload.imu.ax is not None else None,
            "ay": float(payload.imu.ay) if payload.imu and payload.imu.ay is not None else None,
            "az": float(payload.imu.az) if payload.imu and payload.imu.az is not None else None,
            "gx": float(payload.imu.gx) if payload.imu and payload.imu.gx is not None else None,
            "gy": float(payload.imu.gy) if payload.imu and payload.imu.gy is not None else None,
            "gz": float(payload.imu.gz) if payload.imu and payload.imu.gz is not None else None,
        },

        "velocity": {
            "kmh": float(payload.velocity.kmh) if payload.velocity and payload.velocity.kmh is not None else None
        },
    }
    inf_state.ring_buffer.append(msg)

    # Trim old messages by time window (seconds)
    cutoff_epoch = msg["_ts_epoch"] - WINDOW_SECONDS
    while inf_state.ring_buffer and inf_state.ring_buffer[0].get("_ts_epoch", 0.0) < cutoff_epoch:
        inf_state.ring_buffer.popleft()

    # ---- Event mode gate: if not in event mode, stop here (buffer still updated) ----
    if now_sys > getattr(inf_state, "event_until_ts", 0.0):
        return

    # ---- Throttle inference to at most 1Hz ----
    if (now_sys - getattr(inf_state, "last_infer_ts", 0.0)) < 1.0:
        return
    inf_state.last_infer_ts = now_sys

    # Need enough samples
    if len(inf_state.ring_buffer) < MIN_SAMPLES:
        return
    # IMPORTANT: WINDOW_SAMPLES is a count, WINDOW_SECONDS is time duration.
    full_window = list(inf_state.ring_buffer)[-WINDOW_SAMPLES:]

    if len(full_window) > 0:
        sample = full_window[-1]
        print("[DBG] window sample keys:", list(sample.keys()))
        print("[DBG] imu:", sample.get("imu"))
        print("[DBG] velocity:", sample.get("velocity"))

    # Run inference
    result = predict_crash(full_window)
    if not isinstance(result, dict) or "error" in result:
        return

    # warmup on every inference
    inf_state.warmup_counter += 1

    score = result.get("score")
    is_anomaly = bool(result.get("is_anomaly"))
    feats = result.get("features") or {}
    prob = float(result.get("prob", 0.0))
    threshold_used = float(result.get("threshold", 0.0))


    curr_acc_max = float(feats.get("acc_max", 0.0))
    curr_gyro_max = float(feats.get("gyro_max", 0.0))
    curr_speed_max = float(feats.get("speed_max", 0.0))

    # --- LOG EVERY INFERENCE ---
    try:
        print(
            f"[ML] trip={trip_id[-6:]} window={len(full_window)} score={float(score):.3f} "
            f"th={threshold_used:.3f} anomaly={is_anomaly} "
            f"acc_max={curr_acc_max:.2f} gyro_max={curr_gyro_max:.2f} speed_max={curr_speed_max:.1f}"
        )
    except Exception:
        pass

    # -----------------------------
    # Update streak FIRST (fix off-by-one)
    # -----------------------------
    if is_anomaly:
        inf_state.anomaly_streak += 1
    else:
        inf_state.anomaly_streak = 0

    # -----------------------------
    # Adaptive evidence thresholds (unit-aware)
    # -----------------------------
    # If acc magnitudes look like ~1-3, assume "g".
    # If they look like ~9-20, assume "m/s^2".
    acc_is_g = curr_acc_max < 5.0

    # Default floors
    if acc_is_g:
        th_acc = 1.40     # conservative "impact-like" floor in g
        th_gyro = 180.0   # conservative rotation floor (gyro units vary; adjust later)
        min_acc_floor = 1.25
        min_gyro_floor = 120.0
    else:
        th_acc = 12.0     # conservative "impact-like" floor in m/s^2
        th_gyro = 3.5     # legacy floor (only makes sense if your gyro units are small)
        min_acc_floor = 10.5
        min_gyro_floor = 3.5

    # Use percentile history when enough normal samples exist
    try:
        if len(inf_state.normal_acc_max_history) > 10:
            th_acc = float(np.percentile(inf_state.normal_acc_max_history, EVIDENCE_PERCENTILE))
            th_gyro = float(np.percentile(inf_state.normal_gyro_max_history, EVIDENCE_PERCENTILE))
            th_acc = max(th_acc, min_acc_floor)
            th_gyro = max(th_gyro, min_gyro_floor)
    except Exception:
        # keep floors
        pass

    has_acc_spike = curr_acc_max > th_acc
    has_gyro_spike = curr_gyro_max > th_gyro

    # Cooldown
    time_since_alert = now_sys - getattr(inf_state, "last_alert_ts", 0.0)

    confirmed_crash = (
        is_anomaly
        and inf_state.anomaly_streak >= STREAK_MIN
        and inf_state.warmup_counter >= WARMUP_WINDOWS
        and time_since_alert > COOLDOWN_SECONDS
        and (has_acc_spike or has_gyro_spike)
    )

    # Determine label
    if confirmed_crash:
        label = "crash"
    elif is_anomaly:
        label = "anomaly"
    else:
        label = "normal"

    meta_json = {
        "window_len": len(full_window),
        "features": feats,
        "threshold_used": threshold_used,
        "evidence": {
            "acc_max": curr_acc_max,
            "th_acc": th_acc,
            "gyro_max": curr_gyro_max,
            "th_gyro": th_gyro,
            "streak_curr": inf_state.anomaly_streak,
            "streak_min": STREAK_MIN,
            "warmup_curr": inf_state.warmup_counter,
            "warmup_req": WARMUP_WINDOWS,
            "acc_units": "g" if acc_is_g else "mps2",
        },
    }

    # Save prediction (during event mode only)
    async with get_db_context() as pred_db:
        await insert_prediction(
            pred_db,
            device_id=payload.device_id,
            trip_id=trip_id,
            model_name=result.get("model", "unknown"),
            label=label,
            score=float(prob),
            ts=payload.ts,
            meta_json=meta_json,
        )
        await pred_db.commit()

    # Normal history update ONLY when normal (keeps adaptive thresholds meaningful)
    if not is_anomaly:
        try:
            inf_state.normal_acc_max_history.append(curr_acc_max)
            inf_state.normal_gyro_max_history.append(curr_gyro_max)
        except Exception:
            pass
        return

    # Anomaly path logs (optional)
    try:
        print(
            f"[ML] Check Crash: streak={inf_state.anomaly_streak}/{STREAK_MIN} "
            f"warmup={inf_state.warmup_counter}/{WARMUP_WINDOWS} "
            f"acc_spike={has_acc_spike} ({curr_acc_max:.2f}>{th_acc:.2f}) "
            f"gyro_spike={has_gyro_spike} ({curr_gyro_max:.2f}>{th_gyro:.2f})"
        )
    except Exception:
        pass

    if not confirmed_crash:
        return

    # === CONFIRMED CRASH ===
    print(
        f"[Crash] CRASH DETECTED! Streak={inf_state.anomaly_streak} "
        f"Prob={prob:.3f} AccMax={curr_acc_max:.2f} (>{th_acc:.2f}) GyroMax={curr_gyro_max:.2f} (>{th_gyro:.2f})"
    )

    inf_state.last_alert_ts = now_sys

    # Insert alert + broadcast
    async with get_db_context() as alert_db:
        trip_row = await get_trip_by_id(alert_db, trip_id)
        uid = trip_row.user_id if trip_row else None

        if not uid:
            print(f"[Crash][WARN] Crash detected but user_id is None for trip_id={trip_id}. No WS broadcast.")

        alert = await insert_alert(
            alert_db,
            device_id=payload.device_id,
            trip_id=trip_id,
            ts=payload.ts,
            alert_type="crash_server",
            severity="critical",
            message=f"Crash detected (Confidence {prob:.0%})",
            user_id=uid,
            payload_json=result,
        )
        await alert_db.commit()

        if uid:
            broadcast_payload = {
                "type": "ALERT_CRITICAL",
                "payload": {
                    "alert_id": alert.alert_id,
                    "message": alert.message,
                    "device_id": payload.device_id,
                    "ts": payload.ts.isoformat(),
                    "score": prob,
                    "trip_id": trip_id,
                },
            }
            try:
                print(f"[Persist] Broadcasting: {json.dumps(broadcast_payload)}")
            except Exception:
                pass
            await manager.broadcast_to_user(uid, broadcast_payload)




async def _handle_trip_end(payload: TripEndIn) -> None:
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

    _ACTIVE_TRIP.pop(payload.device_id, None)
    _RISK_STATE.pop(trip_id, None)
    _INFERENCE_STATE.pop(trip_id, None)


async def _handle_alert(payload: AlertIn) -> None:
    async with get_db_context() as db:
        trip_id = payload.trip_id or await _resolve_active_trip_id(payload.device_id)

        await insert_alert(
            db,
            device_id=payload.device_id,
            ts=payload.ts,
            alert_type=payload.alert_type.value,
            severity=payload.severity.value,
            message=payload.message,
            user_id=None,
            trip_id=trip_id,
            payload_json=payload.payload,
        )
        await db.commit()
