# app/api/endpoints/users.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from pydantic import BaseModel

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
    # Create user if missing, but don't overwrite profile fields
    user = await UsersRepo.create_user(db, firebase_uid=uid)
    return user


@router.patch("/me", response_model=UserRead)
async def update_my_profile(
    update_data: UserUpdate,
    uid: str = Depends(get_current_user_uid),
    db: AsyncSession = Depends(get_db)
):
    """
    Update current user's profile.
    """
    user = await UsersRepo.update_user(db, uid, **update_data.model_dump(exclude_unset=True))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    return user


@router.post("/logout")
async def logout(
    uid: str = Depends(get_current_user_uid),
):
    """
    Logout user by revoking Firebase refresh tokens.
    This invalidates all existing ID tokens.
    """
    try:
        firebase_auth.revoke_refresh_tokens(uid)
        return {"status": "logged_out"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Logout failed: {str(e)}",
        )