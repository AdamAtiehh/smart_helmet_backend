import asyncio
import httpx
from sqlalchemy import text
from app.database.connection import engine

BASE_URL = "http://127.0.0.1:8000"

async def verify_system():
    # 1. Check API Health
    print("Checking API health...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{BASE_URL}/health")
            if resp.status_code == 200:
                print("✅ API is healthy")
            else:
                print(f"❌ API returned {resp.status_code}")
                return
        except Exception as e:
            print(f"❌ Failed to reach API: {e}")
            print("Make sure uvicorn is running!")
            return

    # 2. Check Initial DB Count
    print("Checking initial DB state...")
    async with engine.begin() as conn:
        res = await conn.execute(text("SELECT COUNT(*) FROM trip_data"))
        initial_count = res.scalar()
    print(f"Initial trip_data count: {initial_count}")

    # 3. Start Mock Sender
    print("Starting mock sender...")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/api/v1/mock/start")
        print(f"Mock start response: {resp.json()}")

    # 4. Wait for data
    print("Waiting 5 seconds for data ingestion...")
    await asyncio.sleep(5)

    # 5. Stop Mock Sender
    print("Stopping mock sender...")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/api/v1/mock/stop")
        print(f"Mock stop response: {resp.json()}")

    # 6. Verify Persistence
    print("Verifying persistence...")
    async with engine.begin() as conn:
        res = await conn.execute(text("SELECT COUNT(*) FROM trip_data"))
        final_count = res.scalar()
    
    print(f"Final trip_data count: {final_count}")
    
    if final_count > initial_count:
        print(f"✅ SUCCESS: Stored {final_count - initial_count} new telemetry rows.")
    else:
        print("❌ FAILURE: No new rows found.")

if __name__ == "__main__":
    # Install httpx if needed: pip install httpx
    # But since we are inside agent, we might not have it.
    # We will assume user has it or we can fallback to urllib if this fails.
    try:
        asyncio.run(verify_system())
    except ImportError:
        print("❌ httpx not installed. Please install it or use curl manually.")
