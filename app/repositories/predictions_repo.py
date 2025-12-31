from __future__ import annotations
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Prediction

async def insert_prediction(
    db: AsyncSession,
    *,
    device_id: str,
    model_name: str,
    label: str,
    score: float,
    trip_id: Optional[str] = None,
    ts: Optional[datetime] = None,
    meta_json: Optional[dict] = None,
) -> Prediction:
    """
    Insert a prediction record.
    """
    if ts is None:
        ts = datetime.utcnow()
        
    row = Prediction(
        device_id=device_id,
        trip_id=trip_id,
        ts=ts,
        model_name=model_name,
        label=label,
        score=score,
        meta_json=meta_json,
    )
    db.add(row)
    await db.flush()
    return row

async def get_predictions_for_trip(
    db: AsyncSession,
    trip_id: str,
) -> Sequence[Prediction]:
    q = select(Prediction).where(Prediction.trip_id == trip_id).order_by(Prediction.ts.asc())
    res = await db.execute(q)
    return tuple(res.scalars().all())
