"""Phase 6 trends: trend_facts indexes.

Run with:
  python migrations/006_trends.py

Existing approved/corrected RCAs can then be loaded into trends with
POST /api/v1/projects/{project_id}/trends/rebuild.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import settings
from libs.common.database import create_mongo_client
from services.trends_service.app.repository import ensure_trend_indexes


async def migrate() -> None:
    client: AsyncIOMotorClient[dict[str, Any]] | None = None
    try:
        client = create_mongo_client(settings.mongodb_uri)
        database = client[settings.mongodb_db]
        await ensure_trend_indexes(database)
        await database["schema_migrations"].update_one(
            {"_id": "006_trends"},
            {"$setOnInsert": {"applied_at": datetime.now(UTC)}},
            upsert=True,
        )
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
