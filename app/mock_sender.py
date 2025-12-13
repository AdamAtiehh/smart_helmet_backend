import asyncio
import json
import websockets
from datetime import datetime, timezone
import random
import urllib.request
import urllib.error
import time
import os
import signal

STOP = False

def handle_stop_signal(signum, frame):
    global STOP
    STOP = True

signal.signal(signal.SIGTERM, handle_stop_signal)
signal.signal(signal.SIGINT, handle_stop_signal)


# Configurable backend URL
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
TEST_TOKEN = os.getenv("TEST_TOKEN")
DEVICE_ID = os.getenv("DEVICE_ID")

# Derive WebSocket URL
if BACKEND_URL.startswith("https://"):
    WS_URL = BACKEND_URL.replace("https://", "wss://")
elif BACKEND_URL.startswith("http://"):
    WS_URL = BACKEND_URL.replace("http://", "ws://")
else:
    # Fallback/Assume plain domain or IP
    WS_URL = f"ws://{BACKEND_URL}"

def make_request(url, method="GET", data=None, headers=None):
    if headers is None:
        headers = {}
    
    req = urllib.request.Request(url, method=method, headers=headers)
    if data:
        req.data = json.dumps(data).encode('utf-8')
        req.add_header('Content-Type', 'application/json')
        
    try:
        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8')
    except urllib.error.URLError as e:
        print(f"Request to {url} failed: {e}")
        raise

async def main():
    if not TEST_TOKEN:
        print("❌ ERROR: TEST_TOKEN environment variable is required.")
        return
    if not DEVICE_ID:
        print("❌ ERROR: DEVICE_ID environment variable is required.")
        return

    # Wait for server to be ready
    print(f"Target Backend: {BACKEND_URL}")
    print("Waiting for server...")
    await asyncio.sleep(2)

    # 1. Ensure user exists (login)
    try:
        print("Logging in...")
        make_request(
            f"{BACKEND_URL}/api/v1/users/me",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        print("Login successful.")
    except Exception as e:
        print(f"Login failed: {e}")
        return # Exit if login fails

    # 2. Register device to user so dashboard receives data
    try:
        print("Registering device...")
        make_request(
            f"{BACKEND_URL}/api/v1/devices",
            method="POST",
            data={"device_id": DEVICE_ID, "model_name": "Helmet v1"},
            headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        print("Device registered.")
    except Exception as e:
        print(f"Registration failed: {e}")

    # 3. Send trip_start
    uri = f"{WS_URL}/ws/ingest"
    print(f"Connecting to {uri}...")
    async with websockets.connect(uri, max_size=64 * 1024) as ws:
        start_msg = {
            "type": "trip_start",
            "device_id": DEVICE_ID,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        await ws.send(json.dumps(start_msg))
        print(f"Sent trip_start: {await ws.recv()}")

        try:
            while not STOP:
                ts_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S")

                msg = {
                    "ts": ts_str,
                    "type": "telemetry",
                    "device_id": DEVICE_ID,
                    "helmet_on": True,
                    "heart_rate": {
                        "ok": True, "ir": 55321, "red": 24123, "finger": True,
                        "hr": int(random.uniform(70, 120)), "spo2": 97
                    },
                    "imu": {
                        "ok": True, "sleep": False,
                        "ax": random.uniform(-0.5, 0.5),
                        "ay": random.uniform(-0.5, 0.5),
                        "az": random.uniform(9.0, 10.0),
                        "gx": 2.0, "gy": 3.0, "gz": 4.0
                    },
                    "gps": {
                        "ok": True,
                        "lat": 33.8547 + random.uniform(-0.001, 0.001),
                        "lng": 35.8623 + random.uniform(-0.001, 0.001),
                        "alt": 12.3, "sats": 8, "lock": True
                    },
                    "crash_flag": False 
                }

                await ws.send(json.dumps(msg))
                resp = await ws.recv()
                print(f"Sent: {resp}")
                await asyncio.sleep(0.2)

        finally:
            # Always send trip_end when exiting gracefully
            end_msg = {
                "type": "trip_end",
                "device_id": DEVICE_ID,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await ws.send(json.dumps(end_msg))
                print(f"Sent trip_end: {await ws.recv()}")
            except Exception as e:
                print(f"Could not send trip_end (connection closed): {e}")


if __name__ == "__main__":
    asyncio.run(main())
