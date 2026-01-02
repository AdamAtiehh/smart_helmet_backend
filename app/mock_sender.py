#app/mock_sender.py

import asyncio
import json
import math
import os
import random
import signal
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

STOP = False


def handle_stop_signal(signum, frame):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, handle_stop_signal)
signal.signal(signal.SIGINT, handle_stop_signal)

# -----------------------------
# Config
# -----------------------------
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
TEST_TOKEN = os.getenv("TEST_TOKEN")
DEVICE_ID = os.getenv("DEVICE_ID")

DT = float(os.getenv("DT", "0.2"))  # seconds

ENABLE_CRASH = os.getenv("ENABLE_CRASH", "1").strip() not in ("0", "false", "False")
CRASH_MIN_SECONDS = float(os.getenv("CRASH_MIN_SECONDS", "75"))
CRASH_CHANCE_PER_TICK = float(os.getenv("CRASH_CHANCE_PER_TICK", "0.012"))

# Derive WebSocket URL
if BACKEND_URL.startswith("https://"):
    WS_BASE = BACKEND_URL.replace("https://", "wss://", 1)
elif BACKEND_URL.startswith("http://"):
    WS_BASE = BACKEND_URL.replace("http://", "ws://", 1)
else:
    WS_BASE = f"ws://{BACKEND_URL}"


# -----------------------------
# HTTP helper (login/register)
# -----------------------------
def make_request(url, method="GET", data=None, headers=None, timeout=10):
    if headers is None:
        headers = {}

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}

    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        print(f"HTTPError {e.code} for {url}: {err_body}")
        raise
    except urllib.error.URLError as e:
        print(f"Request to {url} failed: {e}")
        raise


# -----------------------------
# Geo helpers
# -----------------------------
def meters_to_lat(m: float) -> float:
    return m / 111_320.0


def meters_to_lng(m: float, at_lat: float) -> float:
    denom = 111_320.0 * max(0.2, math.cos(math.radians(at_lat)))
    return m / denom


# -----------------------------
# WS ACK helper
# -----------------------------
async def safe_recv_ack(ws, timeout=3.0) -> str:
    """
    Your /ws/ingest sends an ACK ("✅ saved") per message.
    Avoid blocking forever if something changes server-side.
    """
    try:
        return await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return "⚠️ no-ack (timeout)"


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# -----------------------------
# Driving model
# -----------------------------
def update_speed(
    current_kmh: float,
    target_kmh: float,
    dt: float,
    accel_limit: float,
    decel_limit: float,
) -> float:
    """
    Smooth speed changes (human-like). Limits how fast speed can rise/fall per second.
    """
    delta = target_kmh - current_kmh
    max_up = accel_limit * dt
    max_down = decel_limit * dt

    if delta > 0:
        delta = min(delta, max_up)
    else:
        delta = max(delta, -max_down)

    return current_kmh + delta


def choose_phase(elapsed_s: float) -> Tuple[str, float, Tuple[int, int], float, float, float, Tuple[float, float]]:
    """
    Timeline phases:
    - Start normal city riding
    - Short risky/swerving segment
    - Speeding highway segment
    - Stress/high HR + swerving
    - Back to normal (if no crash ended the run)
    Returns:
      (phase_name, base_target_speed, hr_base_range, gyro_base, gyro_noise, accel_lat, yaw_rate_range)
    """
    if elapsed_s < 25:
        return ("NORMAL", 28.0, (72, 92), 0.10, 0.06, 0.35, (-0.06, 0.06))
    if elapsed_s < 45:
        return ("CITY", 22.0, (70, 95), 0.12, 0.07, 0.40, (-0.10, 0.10))
    if elapsed_s < 65:
        return ("RISKY_TILT", 36.0, (85, 110), 4.0, 1.1, 2.0, (-0.65, 0.65))
    if elapsed_s < 90:
        return ("SPEEDING", 92.0, (88, 115), 0.18, 0.12, 0.75, (-0.10, 0.10))
    if elapsed_s < 120:
        return ("STRESS_SWERVE", 44.0, (125, 160), 4.8, 1.4, 2.4, (-0.80, 0.80))
    return ("NORMAL_AGAIN", 26.0, (72, 92), 0.10, 0.06, 0.35, (-0.06, 0.06))


def maybe_start_event(phase: str, in_crash: bool) -> Optional[str]:
    """
    Random short events that make the ride feel human.
    """
    if in_crash:
        return None

    roll = random.random()

    # More events in city; fewer on highway.
    if phase in ("CITY", "NORMAL"):
        if roll < 0.030:
            return "BRAKE"
        if roll < 0.045:
            return "STOP"
        if roll < 0.060:
            return "BUMP"
        if roll < 0.075:
            return "OVERTAKE"
        return None

    if phase in ("SPEEDING",):
        if roll < 0.015:
            return "BRAKE"
        if roll < 0.030:
            return "OVERTAKE"
        if roll < 0.040:
            return "BUMP"
        return None

    # swerving segments
    if phase in ("RISKY_TILT", "STRESS_SWERVE"):
        if roll < 0.020:
            return "BRAKE"
        if roll < 0.040:
            return "BUMP"
        if roll < 0.060:
            return "OVERTAKE"
        return None

    return None


def synth_heart_rate(
    hr_base: Tuple[int, int],
    speed_kmh: float,
    phase: str,
    event_type: Optional[str],
    in_crash: bool,
) -> int:
    """
    HR is not random-only: it reacts to speed, risky phases, and events.
    """
    hr = int(random.uniform(hr_base[0], hr_base[1]))

    # speed influence
    hr += int((speed_kmh / 120.0) * random.uniform(0, 12))

    # event influence
    if event_type == "OVERTAKE":
        hr += random.randint(4, 12)
    if event_type in ("BRAKE", "STOP") and phase in ("SPEEDING", "RISKY_TILT", "STRESS_SWERVE"):
        hr += random.randint(1, 6)

    # crash influence
    if in_crash:
        hr = int(random.uniform(95, 145))

    return int(clamp(hr, 55, 190))


def synth_imu(
    gyro_base: float,
    gyro_noise: float,
    accel_lat: float,
    phase: str,
    event_type: Optional[str],
    in_crash: bool,
    crash_first_impact: bool,
) -> Tuple[float, float, float, float, float, float]:
    """
    Produces ax, ay, az, gx, gy, gz.
    - Normal: small noise around gravity.
    - Risky phases: higher gyro + lateral accel.
    - BUMP: short az spike + lateral kick.
    - Crash: big spike and chaotic rotation.
    """
    ax = random.uniform(-accel_lat, accel_lat)
    ay = random.uniform(-accel_lat, accel_lat)
    az = random.uniform(9.4, 10.2)

    wiggle = math.sin(time.time() * 1.2)
    gx = (gyro_base * wiggle) + random.uniform(-gyro_noise, gyro_noise)
    gy = (gyro_base * (1 - abs(wiggle))) + random.uniform(-gyro_noise, gyro_noise)
    gz = (gyro_base * 0.5 * wiggle) + random.uniform(-gyro_noise, gyro_noise)

    if event_type == "BUMP":
        ax += random.uniform(-1.5, 1.5)
        ay += random.uniform(-1.5, 1.5)
        az += random.uniform(0.8, 2.5)

    if in_crash:
        if crash_first_impact:
            spike = random.uniform(12, 20)
            ax += spike * random.choice([-1, 1])
            ay += spike * random.choice([-1, 1])
            az += random.uniform(-8, 8)

            gx += random.uniform(6, 12)
            gy += random.uniform(6, 12)
            gz += random.uniform(6, 12)
        else:
            ax += random.uniform(-6, 6)
            ay += random.uniform(-6, 6)
            az += random.uniform(-6, 6)

            gx += random.uniform(1.5, 6)
            gy += random.uniform(1.5, 6)
            gz += random.uniform(1.5, 6)

    return ax, ay, az, gx, gy, gz


def update_gps(
    lat: float,
    lng: float,
    heading: float,
    speed_kmh: float,
    dt: float,
    yaw_rate: float,
    jitter_m: float = 0.8,
) -> Tuple[float, float, float, float, float]:
    """
    Updates lat/lng based on speed + heading.
    Adds small GPS jitter so it looks like a real receiver.
    """
    heading = heading + yaw_rate * dt

    speed_mps = (speed_kmh * 1000.0) / 3600.0
    dist_m = speed_mps * dt

    dx = dist_m * math.cos(heading)  # east
    dy = dist_m * math.sin(heading)  # north

    lat = lat + meters_to_lat(dy)
    lng = lng + meters_to_lng(dx, lat)

    # jitter
    jx = random.uniform(-jitter_m, jitter_m)
    jy = random.uniform(-jitter_m, jitter_m)
    lat_j = lat + meters_to_lat(jy)
    lng_j = lng + meters_to_lng(jx, lat)

    return lat, lng, heading, lat_j, lng_j


def imu_magnitudes(ax, ay, az, gx, gy, gz) -> Tuple[float, float]:
    acc_mag = math.sqrt(ax * ax + ay * ay + az * az)
    gyro_mag = math.sqrt(gx * gx + gy * gy + gz * gz)
    return acc_mag, gyro_mag


def update_crash_latch(
    *,
    current_latch: int,
    speed_kmh: float,
    acc_mag: float,
    gyro_mag: float,
    allow_trigger: bool,
) -> Tuple[int, bool]:
    """
    Calculates crash_flag based on IMU pattern and latches it for a few frames.
    Why latch? Real systems often keep a trigger high briefly so downstream logic
    doesn't miss it because of timing.
    """
    # If already latched, keep it for remaining frames
    if current_latch > 0:
        return current_latch - 1, True

    if not allow_trigger:
        return 0, False

    # A simple "impact-like" condition:
    # - accel magnitude jumps high (normal is ~9.8 m/s^2)
    # - gyro magnitude also high (rotation/chaos)
    # - speed is non-trivial
    trigger = (acc_mag > 16.0 and gyro_mag > 5.0 and speed_kmh > 8.0)

    if trigger:
        latch_frames = 4  # about 0.8s at DT=0.2
        return latch_frames - 1, True

    return 0, False


# -----------------------------
# Main
# -----------------------------
async def main():
    if not TEST_TOKEN:
        print("❌ ERROR: TEST_TOKEN environment variable is required.")
        return
    if not DEVICE_ID:
        print("❌ ERROR: DEVICE_ID environment variable is required.")
        return

    print(f"Target Backend: {BACKEND_URL}")
    print("Waiting for server...")
    await asyncio.sleep(1)

    # 1) Ensure user exists (login)
    try:
        print("Logging in...")
        make_request(
            f"{BACKEND_URL}/api/v1/users/me",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        print("Login successful.")
    except Exception as e:
        print(f"Login failed: {e}")
        return

    # 2) Register device
    try:
        print("Registering device...")
        make_request(
            f"{BACKEND_URL}/api/v1/devices",
            method="POST",
            data={"device_id": DEVICE_ID, "model_name": "Helmet v1"},
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        print("Device registered.")
    except Exception as e:
        print(f"Registration failed (may already exist): {e}")

    # 3) WebSocket ingest
    uri = f"{WS_BASE}/ws/ingest"
    print(f"Connecting to {uri}...")

    # GPS init (Lebanon-ish)
    lat = 33.8547
    lng = 35.8623
    heading = random.uniform(0, 2 * math.pi)

    # Speed state
    current_speed_kmh = 0.0
    speed_noise_phase = random.uniform(0, 2 * math.pi)

    # Event state
    event_type: Optional[str] = None
    event_until_ts = 0.0

    # Crash director
    crash_active = False
    crash_started_ts = 0.0
    crash_duration_s = 2.0
    crashed_once = False

    # crash_flag latch
    crash_latch = 0

    # for print formatting
    tick = 0
    start_time = time.time()

    ws = None
    try:
        async with websockets.connect(
            uri,
            max_size=64 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            # trip_start
            start_msg = {
                "type": "trip_start",
                "device_id": DEVICE_ID,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            await ws.send(json.dumps(start_msg))
            ack = await safe_recv_ack(ws)
            print(f"Sent trip_start: {ack}")

            while not STOP:
                now = time.time()
                elapsed_s = now - start_time

                ts_iso = datetime.now(timezone.utc).isoformat()

                # Phase
                phase, base_target, hr_base, gyro_base, gyro_noise, accel_lat, yaw_rng = choose_phase(elapsed_s)

                # Start/expire events
                if (event_type is None) or (now >= event_until_ts):
                    event_type = maybe_start_event(phase, crash_active)
                    if event_type == "BRAKE":
                        event_until_ts = now + random.uniform(1.2, 2.8)
                    elif event_type == "STOP":
                        event_until_ts = now + random.uniform(2.5, 6.0)
                    elif event_type == "OVERTAKE":
                        event_until_ts = now + random.uniform(1.6, 3.6)
                    elif event_type == "BUMP":
                        event_until_ts = now + random.uniform(0.2, 0.6)
                    else:
                        event_until_ts = now

                # Target speed changes from events
                target_speed_kmh = base_target
                if event_type == "BRAKE":
                    target_speed_kmh = max(0.0, target_speed_kmh - random.uniform(12, 22))
                elif event_type == "STOP":
                    target_speed_kmh = 0.0
                elif event_type == "OVERTAKE":
                    target_speed_kmh = target_speed_kmh + random.uniform(8, 18)

                # Crash logic: maybe trigger once during risky driving
                risky_now = phase in ("RISKY_TILT", "SPEEDING", "STRESS_SWERVE")
                if (
                    ENABLE_CRASH
                    and not crashed_once
                    and not crash_active
                    and elapsed_s >= CRASH_MIN_SECONDS
                    and risky_now
                ):
                    # A tiny per-tick chance makes it feel "might happen"
                    if random.random() < CRASH_CHANCE_PER_TICK:
                        crash_active = True
                        crash_started_ts = now
                        crashed_once = True

                # If crash active: force target speed down hard
                if crash_active:
                    target_speed_kmh = 0.0

                # Accel/decel limits (phase & events)
                accel_limit = 10.0
                decel_limit = 14.0
                if phase in ("SPEEDING",):
                    accel_limit = 14.0
                    decel_limit = 18.0
                if phase in ("RISKY_TILT", "STRESS_SWERVE"):
                    accel_limit = 12.0
                    decel_limit = 18.0
                if event_type in ("BRAKE", "STOP"):
                    decel_limit = 22.0
                if crash_active:
                    decel_limit = 40.0

                # Smooth speed update
                current_speed_kmh = update_speed(
                    current_speed_kmh,
                    target_speed_kmh,
                    DT,
                    accel_limit,
                    decel_limit,
                )

                # Natural wobble/noise
                wobble = 1.0 * math.sin(tick * 0.15 + speed_noise_phase) + random.uniform(-0.8, 0.8)
                current_speed_kmh = max(0.0, current_speed_kmh + wobble)
                current_speed_kmh = min(current_speed_kmh, 160.0)

                # HR
                hr = synth_heart_rate(hr_base, current_speed_kmh, phase, event_type, crash_active)

                # Yaw rate
                yaw_rate = random.uniform(yaw_rng[0], yaw_rng[1])

                # IMU
                crash_first_impact = crash_active and (now - crash_started_ts) < DT
                ax, ay, az, gx, gy, gz = synth_imu(
                    gyro_base,
                    gyro_noise,
                    accel_lat,
                    phase,
                    event_type,
                    crash_active,
                    crash_first_impact,
                )

                # Magnitudes for crash_flag calculation
                acc_mag, gyro_mag = imu_magnitudes(ax, ay, az, gx, gy, gz)

                # End crash after duration (but you’ll still see some chaotic IMU in that window)
                if crash_active and (now - crash_started_ts) >= crash_duration_s:
                    crash_active = False

                # Calculate + latch crash_flag from IMU pattern
                crash_latch, crash_flag = update_crash_latch(
                    current_latch=crash_latch,
                    speed_kmh=current_speed_kmh,
                    acc_mag=acc_mag,
                    gyro_mag=gyro_mag,
                    allow_trigger=True,
                )

                # GPS update
                lat, lng, heading, lat_j, lng_j = update_gps(
                    lat,
                    lng,
                    heading,
                    current_speed_kmh,
                    DT,
                    yaw_rate,
                    jitter_m=0.8,
                )

                # Build message
                msg = {
                    "ts": ts_iso,
                    "type": "telemetry",
                    "device_id": DEVICE_ID,
                    "helmet_on": True,
                    "heart_rate": {
                        "ok": True,
                        "ir": 55321,
                        "red": 24123,
                        "finger": True,
                        "hr": hr,
                        "spo2": 97,
                    },
                    "imu": {
                        "ok": True,
                        "sleep": False,
                        "ax": ax,
                        "ay": ay,
                        "az": az,
                        "gx": gx,
                        "gy": gy,
                        "gz": gz,
                    },
                    "gps": {
                        "ok": True,
                        "lat": lat_j,
                        "lng": lng_j,
                        "alt": 12.3,
                        "sats": 8,
                        "lock": True,
                    },
                    "velocity": {"kmh": float(round(current_speed_kmh, 2))},
                    "crash_flag": bool(crash_flag),
                }

                await ws.send(json.dumps(msg))
                ack = await safe_recv_ack(ws)

                ev = event_type if event_type else "-"
                if crash_active:
                    phase_print = "CRASH"
                else:
                    phase_print = phase

                print(
                    f"[{phase_print:12}] t={elapsed_s:6.1f}s i={tick:04d} "
                    f"v={current_speed_kmh:6.1f} km/h (target={target_speed_kmh:>5.1f}) "
                    f"ev={ev:8} hr={hr:>3} "
                    f"acc={acc_mag:5.1f} gyro={gyro_mag:5.1f} "
                    f"crash_flag={bool(crash_flag)} -> {ack}"
                )

                tick += 1
                await asyncio.sleep(DT)

            # trip_end
            end_msg = {
                "type": "trip_end",
                "device_id": DEVICE_ID,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            await ws.send(json.dumps(end_msg))
            ack = await safe_recv_ack(ws)
            print(f"Sent trip_end: {ack}")

    except ConnectionClosed as e:
        print(f"❌ WebSocket closed by server: code={e.code}, reason={e.reason}")
        try:
            if ws is not None:
                end_msg = {
                    "type": "trip_end",
                    "device_id": DEVICE_ID,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                await ws.send(json.dumps(end_msg))
        except Exception:
            pass
    except Exception as e:
        print(f"❌ Sender error: {e}")


if __name__ == "__main__":
    asyncio.run(main())


"""
app/mock_sender.py

Simulates a realistic motorcycle ride and streams telemetry into your backend
via /ws/ingest. The ride starts normal, then includes some speeding / swerving /
high HR, and may trigger a crash event where `crash_flag` is CALCULATED from IMU.

---------------------------------------------------------------------------
| Function / Section           | What it does                                |
|-----------------------------|----------------------------------------------|
| make_request()              | Calls HTTP endpoints (login, device register)|
| safe_recv_ack()             | Reads the "✅ saved" ACK with a timeout       |
| meters_to_lat / meters_to_lng | Converts meters to lat/lng deltas          |
| update_speed()              | Smooth acceleration/deceleration model       |
| choose_phase()              | Timeline-based driving behavior              |
| maybe_start_event()         | Random driving events (brake/stop/overtake)  |
| synth_heart_rate()          | HR varies with phase/speed/events/crash      |
| synth_imu()                 | IMU varies with phase/events + crash spikes  |
| update_gps()                | GPS moves based on current speed + jitter    |
| update_crash_latch()        | Calculates/latches crash_flag from IMU        |
| main()                      | Orchestrates everything + websocket streaming |
---------------------------------------------------------------------------

---------------------------------------------------------------------------
| Important knobs (env vars)  | Default | Meaning                             |
|-----------------------------|---------|--------------------------------------|
| BACKEND_URL                 | 127.0.0.1:8000 | Backend HTTP base URL       |
| TEST_TOKEN                  | (required) | Firebase ID token for /users/me |
| DEVICE_ID                   | (required) | Device identifier             |
| DT                          | 0.2     | Sampling interval in seconds         |
| ENABLE_CRASH                | 1       | 1=may crash, 0=never crash           |
| CRASH_MIN_SECONDS           | 75      | Earliest time a crash can happen     |
| CRASH_CHANCE_PER_TICK       | 0.012   | Chance per tick during risky driving |
---------------------------------------------------------------------------

Notes:
- `velocity.kmh` is always sent (your RiskAssessor will use it first).
- `crash_flag` is NOT hardcoded by index anymore: it is calculated and latched
  for a few frames when IMU indicates a realistic impact pattern.
"""