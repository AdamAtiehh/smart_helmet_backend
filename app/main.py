from __future__ import annotations
import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware

from app.models.schemas import TelemetryIn, TripStartIn, TripEndIn
from app.workers.persist_worker import enqueue_persist, start_persist_worker
from app.database.connection import engine
from app.models.db_models import Base
from app.api.api_router import api_router
from app.services.connection_manager import manager
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect
from datetime import datetime, timezone



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

@app.on_event("startup")
async def startup_event():
    # Create tables if not exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Start the persistence worker
    asyncio.create_task(start_persist_worker())

@app.get("/health")
async def health():
    return {"status": "ok"}


from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("app/static/login.html", encoding="utf-8") as f:
        return f.read()

@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket, token: str = Query(None)):
    from app.services.auth import verify_firebase_token
    from app.repositories.users_repo import UsersRepo
    from app.database.connection import get_db_context

    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return

    try:
        decoded = await verify_firebase_token(token)
        firebase_uid = decoded.get("uid")

        async with get_db_context() as db:
            user = await UsersRepo.create_user(db, firebase_uid=firebase_uid)
            user_id = user.user_id

        print(f"[ws_stream] Connected user_id={user_id} (firebase_uid={firebase_uid})")

    except Exception as e:
        print(f"[ws_stream] Auth failed: {repr(e)}")
        await websocket.close(code=1008, reason="Invalid token")
        return

    # ✅ DO NOT accept here
    await manager.connect(websocket, user_id)

    try:
        while True:
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, user_id)


# Simple cache for device ownership to avoid DB hits on every packet
# Map device_id -> user_id
_DEVICE_OWNER_CACHE = {}

@app.websocket("/ws/ingest")
async def ws_ingest(websocket: WebSocket):
    from app.repositories.devices_repo import DevicesRepo
    from app.database.connection import get_db_context

    await websocket.accept()
    last_device_id: str | None = None

    try:
        while True:
            data = await websocket.receive_text()

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
                    try:
                        await websocket.send_text("❌ error: unknown type")
                    except Exception:
                        pass
                    continue

                # 1) enqueue persistence
                await enqueue_persist(obj.model_dump())

                # 2) broadcast to owner (non-blocking)
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

                # 3) ACK (needed by mock sender)
                try:
                    await websocket.send_text("✅ saved")
                except Exception:
                    break

            except WebSocketDisconnect:
                break
            except Exception as e:
                try:
                    await websocket.send_text(f"❌ error: {str(e)}")
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    finally:
        if last_device_id:
            try:
                await enqueue_persist({
                    "type": "trip_end",
                    "device_id": last_device_id,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass


# --- Mock Sender Control ---
import subprocess
import sys
import os
from pydantic import BaseModel

mock_process = None

class MockStartRequest(BaseModel):
    device_id: str
    token: str

@app.post("/api/v1/mock/start")
async def start_mock(req: MockStartRequest):
    global mock_process
    if mock_process and mock_process.poll() is None:
        return {"status": "already running", "pid": mock_process.pid}
    
    # Start the mock sender as a subprocess
    # Pass parameters via Environment Variables
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
