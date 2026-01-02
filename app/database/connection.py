# app/database/connection.py
from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./helmet.db").strip()

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,  # helps MySQL reconnects
)

AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


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


async def wait_for_db(retries: int = 30, delay: float = 2.0) -> None:
    # Only matters for MySQL/Postgres; SQLite is always “up”
    for _ in range(retries):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception:
            await asyncio.sleep(delay)
    raise RuntimeError("Database not reachable after retries")


async def init_db(create_all_callable=None) -> None:
    if create_all_callable is None:
        return
    async with engine.begin() as conn:
        await conn.run_sync(create_all_callable)
