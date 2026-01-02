# app/api/endpoints/devices.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from datetime import datetime

from app.models.schemas import DeviceRead, DeviceCreate
from app.database.connection import get_db
from app.services.auth import get_current_user_uid
from app.repositories.devices_repo import DevicesRepo
from app.repositories.users_repo import UsersRepo

router = APIRouter()

# --- Endpoints ---

@router.get("", response_model=List[DeviceRead])
async def list_my_devices(
    uid: str = Depends(get_current_user_uid),
    db: AsyncSession = Depends(get_db)
):
    """
    List all devices owned by the current user.
    """
    user = await UsersRepo.create_user(db, firebase_uid=uid)
    devices = await DevicesRepo.get_user_devices(db, user.user_id)
    return devices

@router.post("", response_model=DeviceRead)
async def register_device(
    device_in: DeviceCreate,
    uid: str = Depends(get_current_user_uid),
    db: AsyncSession = Depends(get_db)
):
    """
    Register a new device to the user.
    """
    user = await UsersRepo.create_user(db, firebase_uid=uid)

    # check if device exists
    device = await DevicesRepo.get_device(db, device_in.device_id)
    if device:
        if device.user_id and device.user_id != user.user_id:
             raise HTTPException(status_code=400, detail="Device already registered to another user")
        
        # Update owner using internal user_id
        device = await DevicesRepo.update_device(db, device_in.device_id, user_id=user.user_id)
    else:
        # Create new device using internal user_id
        device = await DevicesRepo.create_device(
            db, 
            device_id=device_in.device_id, 
            user_id=user.user_id,
            model_name=device_in.model_name,
            device_serial=device_in.device_serial
        )
    
    return device

@router.get("/{device_id}", response_model=DeviceRead)
async def get_device_details(
    device_id: str,
    uid: str = Depends(get_current_user_uid),
    db: AsyncSession = Depends(get_db)
):
    """
    Get details of a specific device.
    """
    # Resolve internal user_id
    user = await UsersRepo.create_user(db, firebase_uid=uid)

    device = await DevicesRepo.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Security check: ensure user owns the device (compare internal user_ids)
    if device.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to view this device")
        
    return device
