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

# Derive WebSocket URL
if BACKEND_URL.startswith("https://"):
    WS_BASE = BACKEND_URL.replace("https://", "wss://", 1)
elif BACKEND_URL.startswith("http://"):
    WS_BASE = BACKEND_URL.replace("http://", "ws://", 1)
else:
    WS_BASE = f"ws://{BACKEND_URL}"


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


def meters_to_lat(m: float) -> float:
    return m / 111_320.0


def meters_to_lng(m: float, at_lat: float) -> float:
    denom = 111_320.0 * max(0.2, math.cos(math.radians(at_lat)))
    return m / denom


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

    DT = 0.2  # sampling interval (seconds)

    # GPS init (Lebanon-ish)
    lat = 33.8547
    lng = 35.8623
    heading = random.uniform(0, 2 * math.pi)

    # Crash segment (optional)
    crash_start = 260
    crash_len = 10
    crash_flag_len = 4

    # "Human driving" state
    current_speed_kmh = 0.0
    speed_noise_phase = random.uniform(0, 2 * math.pi)

    # Random driving events (brake / stop / bump / quick-accelerate)
    event_type = None
    event_until_i = -1

    # Slightly noisy GPS (like real receivers)
    gps_jitter_m = 0.8  # ~0.8m jitter

    ws = None
    try:
        async with websockets.connect(
            uri,
            max_size=64 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            # Send trip_start
            start_msg = {
                "type": "trip_start",
                "device_id": DEVICE_ID,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            await ws.send(json.dumps(start_msg))
            ack = await safe_recv_ack(ws)
            print(f"Sent trip_start: {ack}")

            i = 0
            while not STOP:
                ts_iso = datetime.now(timezone.utc).isoformat()

                # Crash window flags (based only on i)
                in_crash = crash_start <= i < (crash_start + crash_len)
                in_flag = crash_start <= i < (crash_start + crash_flag_len)

                # -----------------------------
                # PHASES (scenario-level behavior)
                # -----------------------------
                # Note: these are BASE targets, we still add “human” variation below.
                if i < 50:
                    phase = "NORMAL"
                    target_speed_kmh = 32
                    hr_base = (75, 95)
                    gyro_base = 0.10
                    gyro_noise = 0.06
                    accel_lat = 0.35
                    yaw_rate = random.uniform(-0.06, 0.06)

                elif i < 100:
                    phase = "RISKY_TILT"
                    target_speed_kmh = 35
                    hr_base = (85, 110)
                    gyro_base = 4.0
                    gyro_noise = 1.1
                    accel_lat = 2.0
                    yaw_rate = random.uniform(-0.65, 0.65)

                elif i < 150:
                    phase = "SPEEDING"
                    target_speed_kmh = 88
                    hr_base = (85, 110)
                    gyro_base = 0.18
                    gyro_noise = 0.12
                    accel_lat = 0.75
                    yaw_rate = random.uniform(-0.10, 0.10)

                elif i < 200:
                    phase = "HIGH_HR"
                    target_speed_kmh = 38
                    hr_base = (125, 155)
                    gyro_base = 0.14
                    gyro_noise = 0.08
                    accel_lat = 0.45
                    yaw_rate = random.uniform(-0.07, 0.07)

                elif i < 250:
                    phase = "DANGEROUS"
                    target_speed_kmh = 105
                    hr_base = (130, 165)
                    gyro_base = 6.0
                    gyro_noise = 2.2
                    accel_lat = 3.5
                    yaw_rate = random.uniform(-0.95, 0.95)

                else:
                    phase = "NORMAL_AGAIN"
                    target_speed_kmh = 28
                    hr_base = (75, 95)
                    gyro_base = 0.10
                    gyro_noise = 0.06
                    accel_lat = 0.35
                    yaw_rate = random.uniform(-0.06, 0.06)

                # -----------------------------
                # Random event generator (more “human”)
                # - short brake
                # - red-light stop
                # - small bump/pothole
                # - quick accelerate (overtake)
                # -----------------------------
                if event_type is None or i > event_until_i:
                    event_type = None
                    # Don’t spam events; keep them rare
                    roll = random.random()
                    if not in_crash:
                        if roll < 0.015:
                            event_type = "BRAKE"
                            event_until_i = i + random.randint(6, 14)  # ~1.2–2.8s
                        elif roll < 0.025:
                            event_type = "STOP"
                            event_until_i = i + random.randint(12, 30)  # ~2.4–6s
                        elif roll < 0.040:
                            event_type = "OVERTAKE"
                            event_until_i = i + random.randint(8, 18)  # ~1.6–3.6s
                        elif roll < 0.060:
                            event_type = "BUMP"
                            event_until_i = i + random.randint(1, 3)  # ~0.2–0.6s

                # Apply event effects to target speed
                if event_type == "BRAKE":
                    target_speed_kmh = max(0, target_speed_kmh - random.uniform(12, 22))
                elif event_type == "STOP":
                    target_speed_kmh = 0
                elif event_type == "OVERTAKE":
                    target_speed_kmh = target_speed_kmh + random.uniform(8, 18)
                # BUMP doesn’t change speed target, it changes IMU later

                # If crash: force speed target down hard
                if in_crash:
                    target_speed_kmh = 0

                # -----------------------------
                # Smooth velocity (ramp up/down)
                # -----------------------------
                accel_limit = 10.0   # km/h per second
                decel_limit = 14.0   # braking is usually faster

                if phase in ("SPEEDING", "DANGEROUS"):
                    accel_limit = 14.0
                    decel_limit = 18.0

                if event_type in ("BRAKE", "STOP"):
                    decel_limit = 22.0

                if in_crash:
                    decel_limit = 40.0

                delta = target_speed_kmh - current_speed_kmh
                max_up = accel_limit * DT
                max_down = decel_limit * DT

                if delta > 0:
                    delta = min(delta, max_up)
                else:
                    delta = max(delta, -max_down)

                current_speed_kmh += delta

                # Small realistic wobble + noise
                wobble = 1.0 * math.sin(i * 0.15 + speed_noise_phase) + random.uniform(-0.8, 0.8)
                current_speed_kmh = max(0.0, current_speed_kmh + wobble)

                # Clamp
                current_speed_kmh = min(current_speed_kmh, 160.0)

                # -----------------------------
                # Heart rate: loosely correlated with risk + speed + events
                # -----------------------------
                hr = int(random.uniform(hr_base[0], hr_base[1]))

                # Add a small speed influence
                hr += int((current_speed_kmh / 120.0) * random.uniform(0, 12))

                # Events can spike HR a bit
                if event_type == "OVERTAKE":
                    hr += random.randint(3, 10)
                if in_crash:
                    hr = int(random.uniform(95, 140))

                hr = int(clamp(hr, 55, 190))

                # -----------------------------
                # IMU baseline
                # -----------------------------
                ax = random.uniform(-accel_lat, accel_lat)
                ay = random.uniform(-accel_lat, accel_lat)
                az = random.uniform(9.4, 10.2)

                wiggle = math.sin(i * 0.2)
                gx = (gyro_base * wiggle) + random.uniform(-gyro_noise, gyro_noise)
                gy = (gyro_base * (1 - abs(wiggle))) + random.uniform(-gyro_noise, gyro_noise)
                gz = (gyro_base * 0.5 * wiggle) + random.uniform(-gyro_noise, gyro_noise)

                # Human-like small bumps
                if event_type == "BUMP":
                    ax += random.uniform(-1.5, 1.5)
                    ay += random.uniform(-1.5, 1.5)
                    az += random.uniform(0.8, 2.5)  # upward spike

                # Crash: inject impact spike + chaos
                if in_crash:
                    phase = "CRASH"
                    if i == crash_start:
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

                # -----------------------------
                # GPS movement based on *current_speed_kmh*
                # -----------------------------
                # Turning slightly depends on speed (faster -> smoother direction changes)
                heading += yaw_rate * DT

                speed_mps = (current_speed_kmh * 1000.0) / 3600.0
                dist_m = speed_mps * DT

                dx = dist_m * math.cos(heading)  # east (m)
                dy = dist_m * math.sin(heading)  # north (m)

                lat += meters_to_lat(dy)
                lng += meters_to_lng(dx, lat)

                # GPS jitter (meters -> lat/lng)
                jx = random.uniform(-gps_jitter_m, gps_jitter_m)
                jy = random.uniform(-gps_jitter_m, gps_jitter_m)
                lat_j = lat + meters_to_lat(jy)
                lng_j = lng + meters_to_lng(jx, lat)

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
                    "velocity": {
                        "kmh": float(round(current_speed_kmh, 2))
                    },
                    "crash_flag": bool(in_flag),
                }

                await ws.send(json.dumps(msg))
                ack = await safe_recv_ack(ws)

                ev = event_type if event_type else "-"
                print(
                    f"[{phase:12}] i={i:03d} v={current_speed_kmh:6.1f} km/h "
                    f"(target={target_speed_kmh:>5.1f}) ev={ev:8} "
                    f"hr={hr:>3} crash_flag={bool(in_flag)} -> {ack}"
                )

                i += 1
                await asyncio.sleep(DT)

            # graceful trip_end
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
        # Try to send trip_end if possible (often you can’t after close)
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
