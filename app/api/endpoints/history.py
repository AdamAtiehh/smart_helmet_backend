from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date as date_cls

from app.database.connection import get_db
from app.services.auth import get_current_user_uid
from app.repositories.users_repo import UsersRepo
from app.repositories.trips_repo import TripsRepo

router = APIRouter()

@router.get("/daily")
async def get_daily_history(
    date: date_cls,
    uid: str = Depends(get_current_user_uid),
    db: AsyncSession = Depends(get_db)
):
    """
    Get aggregated history for a specific date (YYYY-MM-DD).
    """
    # Resolve internal user_id
    user = await UsersRepo.create_user(db, firebase_uid=uid)
    
    # Date will be passed as YYYY-MM-DD string to repo
    stats = await TripsRepo.get_daily_aggregates(db, user.user_id, date)
    return stats

