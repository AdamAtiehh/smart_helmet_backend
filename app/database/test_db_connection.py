# test_db_connection.py
import asyncio
from sqlalchemy import text
from app.database.connection import engine

async def test_connection():
    try:
        async with engine.begin() as conn:
            val = await conn.scalar(text("SELECT 1"))
            print("✅ Database connected successfully! Result:", val)
    except Exception as e:
        print("❌ Database connection failed:", e)
    finally:
        # Dispose engine so no background IO tries to run after loop closes
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(test_connection())
