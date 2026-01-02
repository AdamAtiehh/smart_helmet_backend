import time
from typing import List, Dict
from fastapi import WebSocket

class ConnectionManager:
    """
    Manages active WebSocket connections for real-time streaming.
    Includes throttling to prevent client flooding.
    """
    THROTTLE_INTERVAL = 0.1  # 100ms between messages per user (max 10 msg/sec)

    def __init__(self):
        # Map user_id -> list of sockets
        self.user_connections: Dict[str, List[WebSocket]] = {}
        
        # Map user_id -> timestamp of last sent message
        self.user_last_sent: Dict[str, float] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.user_connections:
            self.user_connections[user_id] = []
        self.user_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str):
        conns = self.user_connections.get(user_id)
        if not conns:
            return

        if websocket in conns:
            conns.remove(websocket)

        if not conns:
            self.user_connections.pop(user_id, None)
            self.user_last_sent.pop(user_id, None)

    async def broadcast_to_user(self, user_id: str, data: dict):
        if user_id not in self.user_connections:
            return

        now = time.monotonic()
        msg_type = data.get("type")

        # Throttle only telemetry (frames can be dropped)
        if msg_type == "telemetry":
            last_sent = self.user_last_sent.get(user_id, 0)
            if now - last_sent < self.THROTTLE_INTERVAL:
                return
            self.user_last_sent[user_id] = now

        for connection in list(self.user_connections[user_id]):
            try:
                await connection.send_json(data)
            except Exception as e:
                print(f"[ConnectionManager] Send failed to user {user_id}: {e}")
                # remove dead socket so it doesn't keep failing forever
                self.disconnect(connection, user_id)

# Global instance
manager = ConnectionManager()
