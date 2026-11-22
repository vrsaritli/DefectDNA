"""Authentication and role indexes.

Run with:
  python migrations/002_authentication.py
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import settings
from libs.common.database import create_mongo_client
from services.api_gateway.app.auth.repository import ensure_auth_indexes


async def migrate() -> None:
    client: AsyncIOMotorClient[dict[str, Any]] | None = None
    try:
        client = create_mongo_client(settings.mongodb_uri)
        database = client[settings.mongodb_db]
        await ensure_auth_indexes(database)
        await database["schema_migrations"].update_one(
            {"_id": "002_authentication"},
            {"$setOnInsert": {"applied_at": datetime.now(UTC)}},
            upsert=True,
        )
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
