from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio

from app.models.schemas import UserRead, UserUpdate
from app.database.connection import get_db
from app.services.auth import get_current_user_uid
from app.repositories.users_repo import UsersRepo
from firebase_admin import auth as firebase_auth

router = APIRouter()

@router.get("/me", response_model=UserRead)
async def get_my_profile(
    uid: str = Depends(get_current_user_uid),
    db: AsyncSession = Depends(get_db)
):
    user = await UsersRepo.create_user(db, firebase_uid=uid)
    return user

@router.patch("/me", response_model=UserRead)
async def update_my_profile(
    update_data: UserUpdate,
    uid: str = Depends(get_current_user_uid),
    db: AsyncSession = Depends(get_db)
):
    user = await UsersRepo.update_user(
        db,
        uid,
        **update_data.model_dump(exclude_unset=True)
    )
    return user

@router.post("/logout")
async def logout(uid: str = Depends(get_current_user_uid)):
    try:
        await asyncio.to_thread(firebase_auth.revoke_refresh_tokens, uid)
        return {"status": "logged_out"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Logout failed: {str(e)}")
