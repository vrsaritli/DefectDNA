"""Phase 4 AI RCA: taxonomy, rca_analyses and LLM-usage indexes; starter taxonomy.

Run with:
  python migrations/004_ai_rca.py
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import settings
from libs.common.database import create_mongo_client
from services.ai_knowledge_service.app.repository import ensure_aik_indexes
from services.api_gateway.app.taxonomy import ensure_taxonomy_indexes, get_current_taxonomy


async def migrate() -> None:
    client: AsyncIOMotorClient[dict[str, Any]] | None = None
    try:
        client = create_mongo_client(settings.mongodb_uri)
        database = client[settings.mongodb_db]
        await ensure_taxonomy_indexes(database)
        await ensure_aik_indexes(database)
        # Give every existing project the starter taxonomy as version 1.
        async for project in database["projects"].find({}, {"_id": 1}):
            await get_current_taxonomy(database, str(project["_id"]))
        await database["schema_migrations"].update_one(
            {"_id": "004_ai_rca"},
            {"$setOnInsert": {"applied_at": datetime.now(UTC)}},
            upsert=True,
        )
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
