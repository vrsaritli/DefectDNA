"""Phase 1 foundation migration.

Run with:
  python migrations/001_foundation.py
"""

import asyncio

from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import Settings


async def migrate() -> None:
    settings = Settings()
    if not settings.mongodb_uri:
        raise RuntimeError("MONGODB_URI is required")

    client = AsyncIOMotorClient(settings.mongodb_uri)
    database = client[settings.mongodb_db]

    for collection_name in ("jobs", "defects", "import_batches", "rca_analyses", "knowledge_entries", "trend_facts"):
        await database[collection_name].create_index("_id")

    await database["jobs"].create_index(
        [("type", 1), ("status", 1), ("lease_expires_at", 1)]
    )
    await database["jobs"].create_index([("created_at", 1)])

    await database.command("ping")
    client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
