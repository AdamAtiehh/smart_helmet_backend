from __future__ import annotations
from datetime import datetime
from typing import Optional, Sequence, Iterable

from sqlalchemy import select, update, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Alert


# -----------------------------
# CREATE
# -----------------------------
async def insert_alert(
    db: AsyncSession,
    *,
    device_id: str,
    ts: datetime,
    alert_type: str,          # use schemas.AlertType values
    severity: str,            # use schemas.Severity values
    message: str,
    user_id: Optional[str] = None,
    trip_id: Optional[str] = None,
    payload_json: Optional[dict] = None,
) -> Alert:
    """
    Insert one alert and return ORM row (not committed).
    """
    row = Alert(
        alert_type=alert_type,
        severity=severity,
        message=message,
        device_id=device_id,
        user_id=user_id,
        trip_id=trip_id,
        ts=ts,
        payload_json=payload_json,
    )
    db.add(row)
    await db.flush()  # populate alert_id
    return row


async def bulk_insert_alerts(db: AsyncSession, rows: Iterable[dict]) -> int:
    """
    Fast path for many alerts. Each dict must match Alert columns.
    Example keys: device_id, ts, alert_type, severity, message, user_id?, trip_id?, payload_json?
    """
    batch = list(rows)
    if not batch:
        return 0
    await db.execute(insert(Alert), batch)
    return len(batch)


# -----------------------------
# READ
# -----------------------------
async def get_by_id(db: AsyncSession, alert_id: str) -> Optional[Alert]:
    res = await db.execute(select(Alert).where(Alert.alert_id == alert_id))
    return res.scalar_one_or_none()


async def recent_for_device(
    db: AsyncSession,
    device_id: str,
    limit: int = 100,
) -> Sequence[Alert]:
    q = (
        select(Alert)
        .where(Alert.device_id == device_id)
        .order_by(Alert.ts.desc())
        .limit(limit)
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())


async def range_for_trip(
    db: AsyncSession,
    trip_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 500,
    offset: int = 0,
) -> Sequence[Alert]:
    conds = [Alert.trip_id == trip_id]
    if start is not None:
        conds.append(Alert.ts >= start)
    if end is not None:
        conds.append(Alert.ts <= end)

    q = (
        select(Alert)
        .where(*conds)
        .order_by(Alert.ts.asc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())


async def recent_for_user(
    db: AsyncSession,
    user_id: str,
    limit: int = 100,
) -> Sequence[Alert]:
    q = (
        select(Alert)
        .where(Alert.user_id == user_id)
        .order_by(Alert.ts.desc())
        .limit(limit)
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())


# -----------------------------
# UPDATE (resolve/ack)
# -----------------------------
async def resolve_alert(
    db: AsyncSession,
    alert_id: str,
    resolved_by: Optional[str] = None,
    resolved_at: Optional[datetime] = None,
) -> None:
    """
    Mark an alert as resolved (e.g., acknowledged by user/support).
    """
    await db.execute(
        update(Alert)
        .where(Alert.alert_id == alert_id)
        .values(
            resolved_at=resolved_at or datetime.utcnow(),
            resolved_by=resolved_by,
        )
    )




# Create from server ML:

# from app.repositories.alerts_repo import insert_alert
# await insert_alert(db,
#     device_id=d_id, ts=ts,
#     alert_type="crash_server", severity="critical",
#     message="High impact + abrupt stop",
#     user_id=user_id, trip_id=trip_id,
#     payload_json={"a_mag": 21.3, "lat": lat, "lng": lng}
# )
# # await db.commit()

# Fetch for device/user, or by trip/time: recent_for_device, recent_for_user, range_for_trip
# Resolve/acknowledge: await resolve_alert(db, alert_id, resolved_by=user_id); await db.commit()
# Repos don’t call commit() — the caller batches operations and commits once for better performance.