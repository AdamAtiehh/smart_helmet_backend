from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.models.db_models import Device, UserDevice


# --------- DEVICE ROWS ---------

async def get_device(db: AsyncSession, device_id: str) -> Optional[Device]:
    """Fetch a device by id, or None if it doesn't exist."""
    res = await db.execute(select(Device).where(Device.device_id == device_id))
    return res.scalar_one_or_none()


async def upsert_device(db: AsyncSession, device_id: str) -> Device:
    """
    Ensure a Device exists (idempotent). Creates it if missing, returns ORM object.
    Caller is responsible for db.commit().
    """
    dev = await get_device(db, device_id)
    if dev is None:
        dev = Device(device_id=device_id)
        db.add(dev)
    return dev


async def update_last_seen(db: AsyncSession, device_id: str, ts: datetime) -> None:
    """Update device heartbeat timestamp (no-op if device doesn't exist)."""
    await db.execute(
        update(Device)
        .where(Device.device_id == device_id)
        .values(last_seen_at=ts)
    )


# --------- OWNERSHIP (USER <-> DEVICE) ---------

async def claim_device_to_user(
    db: AsyncSession,
    user_id: str,
    device_id: str,
    role: str = "owner",
) -> None:
    """
    Link a device to a user (owner/viewer). Safe to call multiple times.
    Creates the Device row if missing.
    """
    await upsert_device(db, device_id)

    link = await db.execute(
        select(UserDevice).where(
            UserDevice.user_id == user_id,
            UserDevice.device_id == device_id,
        )
    )
    link_row = link.scalar_one_or_none()

    if link_row is None:
        # Insert new link; handle race condition with unique constraint gracefully.
        try:
            db.add(UserDevice(user_id=user_id, device_id=device_id, role=role))
            await db.flush()  # push INSERT so IntegrityError happens here if any
        except IntegrityError:
            await db.rollback()
            # Someone else inserted concurrently; update role to be safe.
            await db.execute(
                update(UserDevice)
                .where(UserDevice.user_id == user_id, UserDevice.device_id == device_id)
                .values(role=role)
            )
    else:
        # Link exists; update role if changed.
        if link_row.role != role:
            await db.execute(
                update(UserDevice)
                .where(UserDevice.user_id == user_id, UserDevice.device_id == device_id)
                .values(role=role)
            )

    # IMPORTANT: If role is 'owner', also update the denormalized user_id on the Device table
    # This is what persist_worker uses to assign trips.
    if role == "owner":
        await db.execute(
            update(Device)
            .where(Device.device_id == device_id)
            .values(user_id=user_id)
        )


async def unclaim_device_from_user(db: AsyncSession, user_id: str, device_id: str) -> None:
    """Remove a user<->device link (does not delete the device itself)."""
    await db.execute(
        delete(UserDevice).where(
            UserDevice.user_id == user_id,
            UserDevice.device_id == device_id,
        )
    )


async def list_user_devices(db: AsyncSession, user_id: str) -> Sequence[Device]:
    """
    Return all devices linked to a user, newest first.
    """
    q = (
        select(Device)
        .join(UserDevice, UserDevice.device_id == Device.device_id)
        .where(UserDevice.user_id == user_id)
        .order_by(Device.created_at.desc())
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())


# --- New Methods for API ---

class DevicesRepo:
    """
    Static wrapper class for better import usage in endpoints.
    """
    @staticmethod
    async def get_device(db: AsyncSession, device_id: str) -> Optional[Device]:
        return await get_device(db, device_id)

    @staticmethod
    async def get_user_devices(db: AsyncSession, user_id: str) -> Sequence[Device]:
        return await list_user_devices(db, user_id)

    @staticmethod
    async def create_device(
        db: AsyncSession, 
        device_id: str, 
        user_id: Optional[str] = None,
        model_name: Optional[str] = None,
        device_serial: Optional[str] = None
    ) -> Device:
        dev = Device(
            device_id=device_id, 
            user_id=user_id, 
            model_name=model_name, 
            device_serial=device_serial
        )
        db.add(dev)
        
        if user_id:
            # Also link in user_devices table
            await claim_device_to_user(db, user_id, device_id, role="owner")
            
        await db.commit()
        await db.refresh(dev)
        return dev

    @staticmethod
    async def update_device(
        db: AsyncSession, 
        device_id: str, 
        user_id: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> Device:
        stmt = update(Device).where(Device.device_id == device_id)
        values = {}
        if user_id is not None:
            values["user_id"] = user_id
        if model_name is not None:
            values["model_name"] = model_name
            
        if values:
            await db.execute(stmt.values(**values))
            
        if user_id:
             await claim_device_to_user(db, user_id, device_id, role="owner")
             
        await db.commit()
        return await get_device(db, device_id)