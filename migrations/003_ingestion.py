"""Phase 3 ingestion indexes.

Run with:
  python migrations/003_ingestion.py
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import settings
from libs.common.database import create_mongo_client


async def migrate() -> None:
    client: AsyncIOMotorClient[dict[str, Any]] | None = None
    try:
        client = create_mongo_client(settings.mongodb_uri)
        database = client[settings.mongodb_db]
        await database["defects"].create_index(
            [("project_id", 1), ("ado_id", 1)],
            unique=True,
            name="uq_defects_project_ado_id",
        )
        await database["defects"].create_index(
            [("project_id", 1), ("module", 1), ("release", 1)],
            name="ix_defects_project_module_release",
        )
        await database["import_batches"].create_index(
            [("project_id", 1), ("created_at", -1)],
            name="ix_import_batches_project_created",
        )
        await database["schema_migrations"].update_one(
            {"_id": "003_ingestion"},
            {"$setOnInsert": {"applied_at": datetime.now(UTC)}},
            upsert=True,
        )
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
