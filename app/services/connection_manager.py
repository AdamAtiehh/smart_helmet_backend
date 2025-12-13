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
        if user_id in self.user_connections:
            if websocket in self.user_connections[user_id]:
                self.user_connections[user_id].remove(websocket)
            if not self.user_connections[user_id]:
                del self.user_connections[user_id]
                # Clean up throttle state
                self.user_last_sent.pop(user_id, None)

    async def broadcast_to_user(self, user_id: str, data: dict):
        """
        Send JSON data to a specific user's connections.
        Throttled to prevent flooding.
        """
        if user_id not in self.user_connections:
            return

        now = time.time()
        last_sent = self.user_last_sent.get(user_id, 0)

        # Simple throttling: drop message if too soon
        # Note: For critical alerts, we might want to bypass this or have a separate queue.
        # But for high-freq telemetry, dropping frames is acceptable.
        if now - last_sent < self.THROTTLE_INTERVAL:
            return

        self.user_last_sent[user_id] = now
        
        # Send to all user's sockets
        # Copy list to avoid modification issues during iteration
        for connection in list(self.user_connections[user_id]):
            try:
                await connection.send_json(data)
            except Exception:
                # If send fails, assume disconnect and cleanup later
                pass

# Global instance
manager = ConnectionManager()
