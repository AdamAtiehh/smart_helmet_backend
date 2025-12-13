from __future__ import annotations
from datetime import datetime
from typing import Optional, Sequence, Iterable

from sqlalchemy import select, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import TripData


# -----------------------------
# INSERT ONE SAMPLE
# -----------------------------
async def insert_trip_data(
    db: AsyncSession,
    *,
    device_id: str,
    timestamp: datetime,
    acc_x: float,
    acc_y: float,
    acc_z: float,
    gyro_x: float,
    gyro_y: float,
    gyro_z: float,
    trip_id: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    # speed: Optional[float] = None,
    # accuracy: Optional[float] = None,
    heart_rate: Optional[float] = None,
    # impact_g: Optional[float] = None,
    # battery_pct: Optional[float] = None,
    crash_flag: Optional[bool] = None,
    # raw_payload: Optional[dict] = None,
) -> TripData:
    """
    Insert a single telemetry row (ORM). Caller should commit().
    """
    row = TripData(
        trip_id=trip_id,
        device_id=device_id,
        timestamp=timestamp,
        lat=lat,
        lng=lng,
        # speed=speed,
        # accuracy=accuracy,
        acc_x=acc_x,
        acc_y=acc_y,
        acc_z=acc_z,
        gyro_x=gyro_x,
        gyro_y=gyro_y,
        gyro_z=gyro_z,
        heart_rate=heart_rate,
        # impact_g=impact_g,
        # battery_pct=battery_pct,
        crash_flag=bool(crash_flag) if crash_flag is not None else None,
    )
    db.add(row)
    await db.flush()  # populate data_id
    return row


# -----------------------------
# BULK INSERT (FASTER BATCHES)
# -----------------------------
async def bulk_insert_trip_data(
    db: AsyncSession,
    rows: Iterable[dict],
) -> int:
    """
    Insert many rows at once using a core INSERT (faster).
    Each dict must use TripData column names as keys.
    Returns number of rows attempted (driver may not report exact rowcount).
    Example row keys:
      {
        "trip_id": "...", "device_id": "...", "timestamp": datetime,
        "lat": 33.85, "lng": 35.86, "speed": 12.3, "accuracy": 4.5,
        "acc_x": ..., "acc_y": ..., "acc_z": ...,
        "gyro_x": ..., "gyro_y": ..., "gyro_z": ...,
        "heart_rate": ..., "impact_g": ..., "battery_pct": ..., "crash_flag": ...,
        "raw_payload": {...}
      }
    """
    batch = list(rows)
    if not batch:
        return 0
    await db.execute(insert(TripData), batch)
    # caller decides when to commit
    return len(batch)


# -----------------------------
# READ (HISTORY / RANGE QUERIES)
# -----------------------------
async def get_recent_for_device(
    db: AsyncSession,
    device_id: str,
    limit: int = 200,
) -> Sequence[TripData]:
    q = (
        select(TripData)
        .where(TripData.device_id == device_id)
        .order_by(TripData.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())


async def get_range_for_device(
    db: AsyncSession,
    device_id: str,
    start: datetime,
    end: datetime,
    limit: int = 1000,
    offset: int = 0,
) -> Sequence[TripData]:
    q = (
        select(TripData)
        .where(
            TripData.device_id == device_id,
            TripData.timestamp >= start,
            TripData.timestamp <= end,
        )
        .order_by(TripData.timestamp.asc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())


async def get_range_for_trip(
    db: AsyncSession,
    trip_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 5000,
    offset: int = 0,
) -> Sequence[TripData]:
    conds = [TripData.trip_id == trip_id]
    if start is not None:
        conds.append(TripData.timestamp >= start)
    if end is not None:
        conds.append(TripData.timestamp <= end)

    q = (
        select(TripData)
        .where(*conds)
        .order_by(TripData.timestamp.asc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())



# How this helps (super short)
# insert_trip_data: save one incoming sample (used by your persistence worker).
# bulk_insert_trip_data: save many samples at once (useful if you buffer 100–500 rows for speed).
# get_recent_for_device / get_range_for_*: power your “history” pages and map tracks (fetch by device or by trip and time window).
# Commit is done by the caller (await db.commit()), so you can group multiple writes in one transaction. 