# app/api/api_router.py
from fastapi import APIRouter
from app.api.endpoints import users, devices, trips, alerts

api_router = APIRouter()

api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(devices.router, prefix="/devices", tags=["devices"])
api_router.include_router(trips.router, prefix="/trips", tags=["trips"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
