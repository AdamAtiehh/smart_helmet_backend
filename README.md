# Smart Helmet Backend 🏍️🪖

A real-time backend for a motorcycle safety helmet system built with **FastAPI**, **WebSockets**, and **async SQLAlchemy**.

This service ingests live telemetry (GPS, IMU, heart rate, speed), persists it efficiently using a background queue worker, and streams updates to an authenticated user dashboard in real time. It also includes a risk pipeline and a server-side crash inference flow (ONNX-ready) to support safety alerts.

---

## What this backend does

### ✅ Real-time telemetry ingestion
- The helmet / mobile pipeline sends telemetry to a WebSocket endpoint.
- Incoming data is validated using Pydantic schemas.
- Messages are queued and written to the database by a background worker (so WebSockets stay fast).

### ✅ Live streaming to the dashboard
- Authenticated users connect to a streaming WebSocket endpoint.
- The backend broadcasts telemetry + computed risk status to the connected user in near real time.
- Throttling is applied to avoid flooding the client.

### ✅ Risk & crash logic (server-side)
- **Risk assessor** analyzes a rolling telemetry window and produces a live `RISK_STATUS` feed:
  - speeding
  - swerving patterns (gyro)
  - sudden movement spikes (accel)
  - high heart rate
- **Crash pipeline** uses a state machine + window buffering and is ready for ONNX inference integration.

### ✅ Database support
- Works with **MySQL (production)** and can also use **SQLite (local dev/testing)** depending on your `DATABASE_URL`.
- Telemetry is stored in a flat DB-friendly format for fast reads and charting.

---

## Quick start (local)

### 1) Install dependencies
```bash
pip install -r app/requirements.txt
```

### 2) Configure environment
Create a `.env` file (or export variables):

```env
DATABASE_URL=mysql+aiomysql://USER:PASSWORD@localhost:3306/helmet_db
FIREBASE_CREDENTIALS_PATH=app/firebase/serviceAccountKey.json
```

> If you don’t set `DATABASE_URL`, the backend can be configured to use SQLite depending on your connection settings.

### 3) Run the server
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --workers 1
```

**Important:** keep `--workers 1` because the persistence worker + in-memory state (trip/risk buffers) must run in a single process.

### 4) Open the API docs
- `http://127.0.0.1:8000/docs`

### 5) Open the dashboard
- `http://127.0.0.1:8000/static/dashboard.html`

---

## WebSocket endpoints

### Telemetry ingest (device → backend)
`ws://127.0.0.1:8000/ws/ingest`

The sender posts:
- `trip_start`
- `telemetry`
- `trip_end`

The server replies with an ACK message per frame (used by the mock sender).

### Live stream (backend → user dashboard)
`ws://127.0.0.1:8000/ws/stream?token=FIREBASE_ID_TOKEN`

After auth, the backend streams:
- raw telemetry (throttled)
- `RISK_STATUS`
- critical alerts when triggered (e.g., crash)

---

## Simulate a full ride (recommended demo)

You can simulate a realistic motorcycle ride using the included mock sender:

```bash
export BACKEND_URL="http://127.0.0.1:8000"
export DEVICE_ID="helmet_01"
export TEST_TOKEN="YOUR_FIREBASE_ID_TOKEN"
python app/mock_sender.py
```

The simulation covers normal riding behavior and progressively introduces:
- speeding
- high heart rate
- swerving / aggressive IMU patterns
- and can generate a crash flag window

This is perfect for demos because it produces believable data and triggers risk states naturally.

---

## Authentication

This backend uses **Firebase ID token verification** for user authentication.

- REST endpoints require `Authorization: Bearer <token>`
- `/ws/stream` expects `?token=<token>`

If Firebase credentials are missing or invalid, token verification will fail (expected behavior for secure mode).

---

## Why the architecture is built this way

- **WebSockets must stay responsive** → telemetry persistence happens in a background queue worker.
- **Database writes are continuous** → storing telemetry in a flat structure keeps inserts fast and queries simple.
- **Risk/crash detection needs context** → rolling buffers allow analysis using recent time windows instead of single frames.
- **Single-process worker design** → ensures consistent in-memory state for trip tracking and detection logic.

---

## Roadmap (next improvements)
- Add trip summary aggregation (avg/max speed, distance, HR averages)
- Add ONNX crash inference model training + evaluation
- Add push notifications / emergency contact flow
- Add role-based user/device ownership management for production
- Add charts + richer dashboard UI

---

## License
This project is currently for academic/capstone use. Add a license file if you plan to open-source it.
