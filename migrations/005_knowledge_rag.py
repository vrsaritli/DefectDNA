"""Phase 5 Knowledge Base and RAG migration.

Run with:
  python migrations/005_knowledge_rag.py

Creates the knowledge_entries indexes and, on MongoDB Atlas, the Vector Search index
(``RAG_VECTOR_INDEX_NAME``, ``COHERE_EMBED_DIMENSIONS`` dimensions, cosine) with filter
fields project_id, status and module. On a non-Atlas server the vector index step is
skipped and retrieval falls back to in-process cosine similarity.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from libs.common.config import settings
from libs.common.database import create_mongo_client


def vector_index_definition(dimensions: int) -> dict[str, Any]:
    return {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": dimensions,
                "similarity": "cosine",
            },
            {"type": "filter", "path": "project_id"},
            {"type": "filter", "path": "status"},
            {"type": "filter", "path": "module"},
        ]
    }


async def ensure_phase5_indexes(database: Any) -> None:
    entries = database["knowledge_entries"]
    await entries.create_index(
        [("project_id", 1), ("status", 1), ("updated_at", -1)], name="ix_knowledge_project_status"
    )
    await entries.create_index(
        [("project_id", 1), ("source_rca_id", 1)], unique=True, name="uq_knowledge_project_source"
    )
    await entries.create_index(
        [("project_id", 1), ("defect_id", 1)], name="ix_knowledge_project_defect"
    )


async def ensure_vector_index(database: Any) -> str:
    entries = database["knowledge_entries"]
    name = settings.rag_vector_index_name
    definition = vector_index_definition(settings.cohere_embed_dimensions)
    try:
        existing = [index async for index in entries.list_search_indexes(name)]
        if existing:
            await entries.update_search_index(name, definition)
            return f"updated Atlas vector index '{name}'"
        await entries.create_search_index(
            SearchIndexModel(definition=definition, name=name, type="vectorSearch")
        )
        return f"created Atlas vector index '{name}' (it becomes queryable in about a minute)"
    except OperationFailure as exc:
        return f"skipped Atlas vector index ({exc.code}): not Atlas or search not enabled"


async def migrate() -> None:
    client: AsyncIOMotorClient[dict[str, Any]] | None = None
    try:
        client = create_mongo_client(settings.mongodb_uri)
        database = client[settings.mongodb_db]
        await ensure_phase5_indexes(database)
        print(await ensure_vector_index(database))
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
