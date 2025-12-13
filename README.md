Smart Helmet Backend
--------------------

Backend system for a real-time motorcycle safety helmet using FastAPI, WebSockets, ML inference, and SQLite/MySQL.

This service receives telemetry from the helmet (via Raspberry Pi → mobile app → backend), stores trip data, and streams live updates to the user dashboard. It also includes background workers for data persistence and (later) machine-learning-based crash detection.

---------------------------------------------------
Project Structure
---------------------------------------------------

smart_helmet_backend/

├── app/
│   ├── main.py                 – FastAPI app entrypoint (REST + WebSockets + workers)
│   │
│   ├── models/
│   │   ├── db_models.py        – SQLAlchemy ORM models
│   │   └── schemas.py          – Pydantic request/response models
│   │
│   ├── database/
│   │   └── connection.py       – Async SQLAlchemy engine + session handling
│   │
│   ├── workers/
│   │   ├── persist_worker.py   – Background queue → saves telemetry/trips
│   │   └── inference_worker.py – (Future) ML model inference worker
│   │
│   ├── ml/
│   │   ├── model.onnx          – Placeholder for crash-detection ML model
│   │   └── predictor.py        – Runs ONNX model
│   │
│   ├── services/
│   │   ├── connection_manager.py – Tracks connected WebSocket users
│   │   └── broadcaster.py        – Sends real-time updates to /ws/stream
│   │
│   ├── api/
│   │   └── api_router.py       – Organizes API endpoints (/api/v1)
│   │
│   ├── static/
│   │   └── dashboard.html      – Simple front-end dashboard for debugging
│   │
│   └── mock_sender.py          – Sends fake telemetry for testing

├── helmet.db                   – SQLite database (auto-created)
├── .env                        – Optional: DATABASE_URL, Firebase, ML paths
└── README.md


---------------------------------------------------
Main Features
---------------------------------------------------

1. Real-time Telemetry Ingestion  
   • /ws/ingest receives live telemetry:  
     - GPS  
     - Speed  
     - Accelerometer / Gyroscope  
     - Heart rate (planned)  
     - Stress levels (planned)  
     - Crash-probability signals (ML, planned)  

   • Telemetry is validated, queued, saved to DB, and broadcast.

2. Real-time Dashboard Updates  
   • /ws/stream pushes live updates to the authenticated user  
   • Used for live map tracking, live speed, crash alerts, trip progress  
   • Works with static/dashboard.html

3. Background Workers  
   • Persist Worker → Saves telemetry + trip events without blocking WebSockets  
   • ML Worker (future) → Runs crash detection model and sends alerts

4. Database Flexibility  
   • SQLite (default local development)  
   • MySQL/Postgres (optional for production)

---------------------------------------------------
Local Development
---------------------------------------------------

1. Install dependencies:
   pip install -r requirements.txt

2. Start FastAPI server:
   uvicorn app.main:app --reload

3. Open API documentation:
   http://127.0.0.1:8000/docs

4. View live dashboard:
   http://127.0.0.1:8000/static/dashboard.html


---------------------------------------------------
WebSocket Endpoints
---------------------------------------------------

1. ws://host/ws/ingest  
   → Helmet/mobile app sends telemetry here

2. ws://host/ws/stream?token=USER_TOKEN  
   → Dashboard receives real-time updates


---------------------------------------------------
Simulate Telemetry (for testing)
---------------------------------------------------

Run:
   python app/mock_sender.py

This sends random telemetry events to /ws/ingest.


---------------------------------------------------
Authentication
---------------------------------------------------

• Firebase token verification supported  
• When Firebase credentials are missing → backend switches to MOCK MODE  
  (useful for development and Burp testing)


---------------------------------------------------
Roadmap / Future Work
---------------------------------------------------

• ONNX crash detection model  
• Trip summary statistics  
• User health analytics  
• Admin dashboard (graphs, maps, logs)  
• Push notifications for crash detection  
• More advanced sensor classification
