from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.api_router import api_router
from app.database.connection import init_db, wait_for_db, get_db_context
from app.models.db_models import Base, User
from app.models.schemas import TelemetryIn, TripStartIn, TripEndIn
from app.repositories.devices_repo import DevicesRepo
from app.repositories.users_repo import UsersRepo
from app.services.connection_manager import manager
from app.workers.persist_worker import enqueue_persist, start_persist_worker


# ------------------------------------------------------------------------------
# App
# ------------------------------------------------------------------------------
app = FastAPI(title="Smart Helmet Backend (Test Run)")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(api_router, prefix="/api/v1")


# ------------------------------------------------------------------------------
# Startup / Shutdown
# ------------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    """
    Startup boot sequence:
    1) (Optional) wait for DB readiness (useful for MySQL/Postgres)
    2) create tables (Base.metadata.create_all)
    3) start persistence worker (queue consumer)
    """
    await wait_for_db()
    await init_db(Base.metadata.create_all)
    asyncio.create_task(start_persist_worker())


mock_process: Optional[subprocess.Popen] = None


@app.on_event("shutdown")
async def shutdown_event():
    """
    Best-effort cleanup.
    Important when using --reload, so we don't leave mock sender running.
    """
    global mock_process
    if mock_process and mock_process.poll() is None:
        try:
            mock_process.terminate()
            mock_process.wait(timeout=2)
        except Exception:
            try:
                mock_process.kill()
            except Exception:
                pass
        finally:
            mock_process = None


# ------------------------------------------------------------------------------
# Simple HTTP endpoints
# ------------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("app/static/login.html", encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------------------
# WebSocket: Stream (server -> client)
# Client connects with Firebase token, we map it to internal user_id, then keep
# the socket open. Broadcasts are sent via ConnectionManager.broadcast_to_user().
# ------------------------------------------------------------------------------
@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket, token: str = Query(None)):
    from app.services.auth import verify_firebase_token

    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return

    try:
        decoded = await verify_firebase_token(token)
        firebase_uid = decoded.get("uid")
        email = decoded.get("email")

        if not firebase_uid:
            await websocket.close(code=1008, reason="Invalid token (missing uid)")
            return

        # Ensure user exists; handle possible race on firebase_uid uniqueness.
        async with get_db_context() as db:
            try:
                user = await UsersRepo.create_user(db, firebase_uid=firebase_uid, email=email)
            except IntegrityError:
                # Another request created the same firebase_uid at the same time.
                await db.rollback()
                res = await db.execute(select(User).where(User.firebase_uid == firebase_uid))
                user = res.scalar_one()

            user_id = user.user_id

    except Exception as e:
        print(f"[ws_stream] Auth failed: {repr(e)}")
        await websocket.close(code=1008, reason="Invalid token")
        return

    # manager.connect() will accept() the socket (do NOT accept here)
    await manager.connect(websocket, user_id)

    try:
        # Keep connection alive; client doesn't need to send anything.
        while True:
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, user_id)


# ------------------------------------------------------------------------------
# WebSocket: Ingest (device/app -> server)
# Validates payload, enqueues persistence, broadcasts to owner, sends ACK.
# ------------------------------------------------------------------------------
_DEVICE_OWNER_CACHE: Dict[str, str] = {}  # device_id -> user_id (internal)


@app.websocket("/ws/ingest")
async def ws_ingest(websocket: WebSocket):
    await websocket.accept()
    last_device_id: Optional[str] = None

    try:
        while True:
            try:
                data = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            # Parse + validate
            try:
                payload = json.loads(data)
                msg_type = payload.get("type")
                device_id = payload.get("device_id")

                if device_id:
                    last_device_id = device_id

                if msg_type == "telemetry":
                    obj = TelemetryIn(**payload)
                elif msg_type == "trip_start":
                    obj = TripStartIn(**payload)
                elif msg_type == "trip_end":
                    obj = TripEndIn(**payload)
                else:
                    await websocket.send_text("❌ error: unknown type")
                    continue

                # 1) enqueue persistence (DB work happens in persist_worker)
                await enqueue_persist(obj.model_dump())

                # 2) broadcast to owner (best-effort, non-blocking)
                if device_id:
                    owner_id = _DEVICE_OWNER_CACHE.get(device_id)

                    if not owner_id:
                        async with get_db_context() as db:
                            device = await DevicesRepo.get_device(db, device_id)
                            if device and device.user_id:
                                owner_id = device.user_id
                                _DEVICE_OWNER_CACHE[device_id] = owner_id

                    if owner_id:
                        asyncio.create_task(manager.broadcast_to_user(owner_id, payload))

                # 3) ACK (mock sender expects this per message)
                await websocket.send_text("✅ saved")

            except Exception as e:
                # Send error to sender; if that fails, end the loop.
                try:
                    await websocket.send_text(f"❌ error: {str(e)}")
                except Exception:
                    break

    finally:
        # If device disconnects mid-trip, best-effort trip_end based on last_device_id
        if last_device_id:
            try:
                await enqueue_persist(
                    {
                        "type": "trip_end",
                        "device_id": last_device_id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except Exception:
                pass


# ------------------------------------------------------------------------------
# Mock Sender Control (local testing)
# ------------------------------------------------------------------------------
class MockStartRequest(BaseModel):
    device_id: str
    token: str


@app.post("/api/v1/mock/start")
async def start_mock(req: MockStartRequest):
    global mock_process

    if mock_process and mock_process.poll() is None:
        return {"status": "already running", "pid": mock_process.pid}

    env = os.environ.copy()
    env["DEVICE_ID"] = req.device_id
    env["TEST_TOKEN"] = req.token

    try:
        mock_process = subprocess.Popen([sys.executable, "app/mock_sender.py"], env=env)
        return {"status": "started", "pid": mock_process.pid}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/mock/stop")
async def stop_mock():
    global mock_process

    if mock_process and mock_process.poll() is None:
        mock_process.terminate()
        try:
            mock_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock_process.kill()

        mock_process = None
        return {"status": "stopped"}

    return {"status": "not running"}


# ==============================================================================
# MAIN.PY TABLES (requested)
# ==============================================================================

# | Function / Endpoint        | What it does                                                                 |
# |---------------------------|-------------------------------------------------------------------------------|
# | startup_event()           | Waits for DB, creates tables, starts persist worker background task.          |
# | shutdown_event()          | Stops mock sender if running (helps with uvicorn --reload).                   |
# | GET /health               | Simple health response for quick checks and deployments.                      |
# | GET /                     | Serves app/static/login.html for quick manual testing.                        |
# | WS /ws/stream             | Auth via Firebase token, registers socket under internal user_id, stays open.|
# | WS /ws/ingest             | Receives JSON, validates via schemas, enqueues persist, broadcasts, ACKs.     |
# | POST /api/v1/mock/start   | Spawns app/mock_sender.py with env DEVICE_ID + TEST_TOKEN (local testing).    |
# | POST /api/v1/mock/stop    | Terminates mock sender subprocess.                                            |

################################################################################################

# | Global / Cache           | What it stores                              | Why it exists / Notes                      |
# |-------------------------|----------------------------------------------|--------------------------------------------|
# | _DEVICE_OWNER_CACHE     | device_id -> user_id (internal UUID string)  | Avoid DB lookup for every telemetry frame. |
# | mock_process            | subprocess handle for mock sender            | Allows start/stop endpoints to control it. |

################################################################################################

# | Data Flow Summary |
# |-------------------|
# | Device/App -> WS /ws/ingest -> Pydantic validate -> enqueue_persist() -> persist_worker -> DB |
# | Device/App -> WS /ws/ingest -> owner lookup/cache -> ConnectionManager.broadcast_to_user()   |
# | Client -> WS /ws/stream (Firebase token) -> mapped to internal user_id -> receives broadcasts|
