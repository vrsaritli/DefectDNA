"""Phase 5 Knowledge Base and RAG migration.

Run with:
  python migrations/005_knowledge_rag.py

MongoDB Atlas Vector Search indexes must be created separately in Atlas using
the name and field from ensure_phase5_indexes.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import settings
from libs.common.database import create_mongo_client


async def ensure_phase5_indexes(database: Any) -> None:
    entries = database["knowledge_entries"]
    await entries.create_index([("project_id", 1), ("status", 1), ("updated_at", -1)], name="ix_knowledge_project_status")
    await entries.create_index([("project_id", 1), ("source_rca_id", 1)], unique=True, name="uq_knowledge_project_source")
    await entries.create_index([("project_id", 1), ("defect_id", 1)], name="ix_knowledge_project_defect")


async def migrate() -> None:
    client: AsyncIOMotorClient[dict[str, Any]] | None = None
    try:
        client = create_mongo_client(settings.mongodb_uri)
        database = client[settings.mongodb_db]
        await ensure_phase5_indexes(database)
        await database["schema_migrations"].update_one(
            {"_id": "005_knowledge_rag"},
            {"$setOnInsert": {"applied_at": datetime.now(UTC)}},
            upsert=True,
        )
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
