from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.engine.url import make_url

load_dotenv()

DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///./helmet.db"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL).strip()

def _create_engine(url: str) -> AsyncEngine:
    make_url(url)
    return create_async_engine(url, echo=False, pool_pre_ping=True)

engine: AsyncEngine = _create_engine(DATABASE_URL)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_db() -> AsyncIterator[AsyncSession]:
    session: AsyncSession = AsyncSessionLocal()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()

get_db_context = asynccontextmanager(get_db)

async def init_db(create_all_callable=None) -> None:
    if create_all_callable is None:
        return
    async with engine.begin() as conn:
        await conn.run_sync(create_all_callable)
