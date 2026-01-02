# app/services/auth.py
from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import firebase_admin
from firebase_admin import auth, credentials

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database.connection import get_db
from app.models.schemas import AuthUser
from app.models.db_models import User
from sqlalchemy.exc import IntegrityError


# Initialize Firebase Admin SDK
cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH")

if not firebase_admin._apps:
    if cred_path and os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        print(f"[auth] Firebase Admin initialized with {cred_path}")
    else:
        print("[auth] ERROR: No Firebase credentials found. Firebase Admin is NOT initialized.")

security = HTTPBearer()


async def get_current_user_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials

async def verify_firebase_token(token: str) -> dict:
    if not firebase_admin._apps:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase Admin not initialized. Cannot verify tokens.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        decoded_token = auth.verify_id_token(token, check_revoked=True)
        return decoded_token
    except auth.RevokedIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please login again.",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

async def _get_or_create_user(
    db: AsyncSession,
    *,
    firebase_uid: str,
    email_from_token: Optional[str],
) -> User:
    result = await db.execute(select(User).where(User.firebase_uid == firebase_uid))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(firebase_uid=firebase_uid, email=email_from_token)
        db.add(user)

        try:
            await db.commit()
            await db.refresh(user)
            return user
        except IntegrityError:
            # Another request created the same user at the same time
            await db.rollback()
            result = await db.execute(select(User).where(User.firebase_uid == firebase_uid))
            user = result.scalar_one_or_none()
            if user is None:
                # super rare, but don't hide it
                raise
            return user

    # Only fill email if missing (optional)
    if email_from_token and not user.email:
        user.email = email_from_token
        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
        except IntegrityError:
            # If email is unique and conflicts, don't crash auth
            await db.rollback()

    return user


async def get_current_user(
    token: str = Depends(get_current_user_token),
    db: AsyncSession = Depends(get_db),
) -> AuthUser:
    decoded = await verify_firebase_token(token)

    firebase_uid = decoded.get("uid")
    if not firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token: missing uid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    email: Optional[str] = decoded.get("email")

    db_user = await _get_or_create_user(
        db,
        firebase_uid=firebase_uid,
        email_from_token=email,
    )

    return AuthUser(
        user_id=db_user.user_id,
        firebase_uid=db_user.firebase_uid,
        email=db_user.email,
        display_name=db_user.display_name,
        phone_number=db_user.phone_number,
    )


async def get_current_user_uid(
    current_user: AuthUser = Depends(get_current_user),
) -> str:
    return current_user.firebase_uid
