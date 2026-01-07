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
    db: AsyncSession = Depends(get_db),
):
    """
    Pair/register device to the current user.
    Rules:
      - If device doesn't exist: create it and assign to user.
      - If device exists:
          - If already owned by this user: just update metadata (model_name/serial) if provided.
          - If owned by another user: transfer ownership to this user (single-helmet/single-owner behavior).
    """
    # Ensure user exists
    user = await UsersRepo.create_user(db, firebase_uid=uid)

    # Fetch device
    device = await DevicesRepo.get_device(db, device_in.device_id)

    if device:
        # If device is owned by someone else, we TRANSFER it to this user
        if device.user_id and device.user_id != user.user_id:
            # Transfer ownership (your repo should also update user_devices via claim_device_to_user)
            device = await DevicesRepo.update_device(
                db,
                device_in.device_id,
                user_id=user.user_id,
                model_name=device_in.model_name,
            )
        else:
            # Already owned by this user (or unclaimed) -> ensure it is linked to this user
            device = await DevicesRepo.update_device(
                db,
                device_in.device_id,
                user_id=user.user_id,
                model_name=device_in.model_name,
            )
    else:
        # Create and assign to this user
        device = await DevicesRepo.create_device(
            db,
            device_id=device_in.device_id,
            user_id=user.user_id,
            model_name=device_in.model_name,
            device_serial=getattr(device_in, "device_serial", None),
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
