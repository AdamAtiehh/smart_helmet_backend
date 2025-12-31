# from __future__ import annotations
# import asyncio
# import json
# from app.services.connection_manager import manager
# from app.ml.predictor import ONNXPredictor
# from app.models.schemas import TelemetryIn, AlertIn, AlertType, Severity
# from app.workers.persist_worker import _handle_alert

# # Queue for inference tasks
# _INFERENCE_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=10_000)

# predictor = ONNXPredictor()

# async def enqueue_inference(msg: dict) -> None:
#     """
#     Put a message onto the inference queue.
#     """
#     await _INFERENCE_QUEUE.put(msg)

# async def start_inference_worker() -> None:
#     """
#     Consume messages and run ML inference.
#     """
#     while True:
#         msg = await _INFERENCE_QUEUE.get()
#         try:
#             await _run_inference(msg)
#         except Exception as e:
#             print(f"[inference] error: {e}")
#         finally:
#             _INFERENCE_QUEUE.task_done()

# async def _run_inference(msg: dict) -> None:
#     # Only care about telemetry for now
#     if msg.get("type") != "telemetry":
#         return

#     # 1. Run prediction
#     result = predictor.predict(msg)
    
#     # 2. If positive, generate Alert
#     if result:
#         # Example validation - real logic depends on model output
#         alert_payload = AlertIn(
#             trip_id=msg.get("trip_id"), 
#             device_id=msg.get("device_id"),
#             ts=msg.get("ts"),
#             alert_type=AlertType.CRASH, # or derived from result
#             severity=Severity.critical,
#             message="Crash detected by ML Model",
#             payload=result
#         )
        
#         # Persist alert
#         await _handle_alert(alert_payload)
        
#         # Broadcast alert to FE
#         # We might want to construct a broadcast payload
#         broadcast_msg = {
#             "type": "alert",
#             "device_id": msg.get("device_id"),
#             "alert": alert_payload.model_dump(mode='json')
#         }
#         # Ideally we know the user_id to broadcast to. 
#         # For now, simplistic broadcast or rely on persist_worker logic?
#         # Actually persist worker doesn't broadcast alerts, it just saves.
#         # We should probably broadcast here or in the connection manager.
