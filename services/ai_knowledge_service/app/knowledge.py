"""Knowledge base: confirmed RCAs as embedded entries, and retrieval for RAG.

Texts sent to the embedding provider are scrubbed first, exactly like LLM input.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from libs.common.config import Settings
from libs.common.taxonomy import Taxonomy
from services.ai_knowledge_service.app.adapters.embedding import (
    EmbeddingAdapterError,
    EmbeddingClient,
)
from services.ai_knowledge_service.app.pipeline.scrub import scrub_text
from services.ai_knowledge_service.app.repository import consume_llm_call, retrieve_knowledge

logger = structlog.get_logger()

REEMBED_JOB = "reembed_all"
ACTIVE = "ACTIVE"
PENDING_EMBEDDING = "PENDING_EMBEDDING"
_QUERY_MAX_CHARS = 1_500
_SUMMARY_MAX_CHARS = 240


def _comments(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value)
    return str(value or "")


def defect_text(defect: dict[str, Any], max_chars: int = _QUERY_MAX_CHARS) -> str:
    """Scrubbed defect description used both for queries and for knowledge entries."""
    parts = [
        f"Title: {defect.get('title') or ''}",
        f"Module: {defect.get('module') or ''}",
        f"Description: {defect.get('description') or ''}",
        f"Steps: {defect.get('repro_steps') or ''}",
        f"Comments: {_comments(defect.get('comments'))}",
    ]
    return scrub_text("\n".join(parts)).text[:max_chars]


def knowledge_text(
    defect: dict[str, Any],
    final: dict[str, Any],
    taxonomy: Taxonomy | None,
    note: str | None,
) -> str:
    def label(kind: Any, code: Any) -> str:
        if not code:
            return "unknown"
        return (taxonomy.label(kind, code) if taxonomy else None) or str(code)

    confirmed = (
        f"\nConfirmed root cause: {label('root_causes', final.get('root_cause_code'))}"
        f"\nSDLC phase introduced: {label('sdlc_phases', final.get('sdlc_phase_code'))}"
        f"\nTesting gap: {label('testing_gaps', final.get('testing_gap_code'))}"
    )
    if note:
        confirmed += f"\nReviewer note: {scrub_text(note).text[:400]}"
    return defect_text(defect) + confirmed


def summary_line(defect: dict[str, Any]) -> str:
    text = scrub_text(str(defect.get("description") or defect.get("title") or "")).text
    return " ".join(text.split())[:_SUMMARY_MAX_CHARS]


async def upsert_knowledge_entry(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    rca: dict[str, Any],
    defect: dict[str, Any],
    final: dict[str, Any],
    text: str,
    embedding: list[float] | None,
    embedding_model: str | None,
    source: str,
) -> Any:
    now = datetime.now(UTC)
    snapshot = rca.get("defect_snapshot") or {}
    document = {
        "project_id": rca["project_id"],
        "defect_id": int(rca["defect_id"]),
        "source_rca_id": str(rca["_id"]),
        "source": source,
        "status": ACTIVE if embedding else PENDING_EMBEDDING,
        "title": str(defect.get("title") or snapshot.get("title") or "")[:300],
        "module": str(defect.get("module") or snapshot.get("module") or ""),
        "release": str(defect.get("release") or snapshot.get("release") or ""),
        "summary": summary_line(defect),
        "summary_text": text,
        "root_cause_code": final.get("root_cause_code"),
        "sdlc_phase_code": final.get("sdlc_phase_code"),
        "testing_gap_code": final.get("testing_gap_code"),
        "embedding": embedding,
        "embedding_model": embedding_model if embedding else None,
        "updated_at": now,
    }
    result = await database["knowledge_entries"].find_one_and_update(
        {"project_id": rca["project_id"], "source_rca_id": str(rca["_id"])},
        {"$set": document, "$setOnInsert": {"created_at": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result["_id"] if result else None


async def remove_knowledge_entry(
    database: AsyncIOMotorDatabase[dict[str, Any]], project_id: str, rca_id: str
) -> int:
    result = await database["knowledge_entries"].delete_many(
        {"project_id": project_id, "source_rca_id": rca_id}
    )
    return int(result.deleted_count)


async def vector_search(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    project_id: str,
    query_embedding: list[float],
    settings: Settings,
    exclude_defect_id: int | None = None,
) -> list[dict[str, Any]]:
    """Top-k ACTIVE entries of the project above the similarity threshold (cosine)."""
    limit = settings.rag_top_k + (1 if exclude_defect_id is not None else 0)
    mode = settings.rag_vector_search_mode.strip().casefold()
    results: list[dict[str, Any]] | None = None
    if mode in {"auto", "atlas"}:
        try:
            cursor = database["knowledge_entries"].aggregate(
                [
                    {
                        "$vectorSearch": {
                            "index": settings.rag_vector_index_name,
                            "path": "embedding",
                            "queryVector": query_embedding,
                            "numCandidates": max(100, limit * 20),
                            "limit": limit,
                            "filter": {"project_id": project_id, "status": ACTIVE},
                        }
                    },
                    {"$addFields": {"_score": {"$meta": "vectorSearchScore"}}},
                    {"$project": {"embedding": 0, "summary_text": 0}},
                ]
            )
            results = []
            async for doc in cursor:
                # Atlas normalises cosine to (1 + cos) / 2; convert back to cosine.
                doc["score"] = round(2 * float(doc.pop("_score")) - 1, 6)
                if doc["score"] >= settings.rag_similarity_threshold:
                    results.append(doc)
        except Exception as exc:
            if mode == "atlas":
                raise
            logger.warning("vector_search_fallback", reason=type(exc).__name__)
            results = None
    if results is None:
        results = await retrieve_knowledge(
            database,
            project_id=project_id,
            query_embedding=query_embedding,
            top_k=limit,
            threshold=settings.rag_similarity_threshold,
        )
        for doc in results:
            doc.pop("summary_text", None)
    filtered = [doc for doc in results if doc.get("defect_id") != exclude_defect_id]
    return filtered[: settings.rag_top_k]


def to_case(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "knowledge_entry_id": str(doc.get("_id")),
        "defect_id": doc.get("defect_id"),
        "similarity": doc.get("score"),
        "title": doc.get("title"),
        "module": doc.get("module"),
        "release": doc.get("release"),
        "summary": doc.get("summary"),
        "root_cause_code": doc.get("root_cause_code"),
        "sdlc_phase_code": doc.get("sdlc_phase_code"),
        "testing_gap_code": doc.get("testing_gap_code"),
        "source": doc.get("source"),
    }


@dataclass
class Retrieval:
    status: str
    cases: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    embedding_model: str | None = None


Retriever = Callable[[list[dict[str, Any]]], Awaitable[Retrieval]]


def build_retriever(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    embedder: EmbeddingClient | None,
    settings: Settings,
    project_id: str,
) -> Retriever:
    """Embed each defect (query mode) and fetch similar confirmed cases.

    Failures degrade to "no cases" with a recorded status; they never block analysis.
    """

    async def retrieve(defects: list[dict[str, Any]]) -> Retrieval:
        if embedder is None:
            return Retrieval(status="DISABLED")
        if not await database["knowledge_entries"].count_documents(
            {"project_id": project_id, "status": ACTIVE}, limit=1
        ):
            return Retrieval(status="EMPTY_KNOWLEDGE_BASE", embedding_model=embedder.model)
        if not await consume_llm_call(database, f"embed:{project_id}", settings.embed_daily_call_cap):
            return Retrieval(status="EMBED_DAILY_CAP_REACHED", embedding_model=embedder.model)
        try:
            vectors = await embedder.embed([defect_text(d) for d in defects], "search_query")
        except EmbeddingAdapterError as exc:
            logger.warning("rag_query_embedding_failed", code=exc.code)
            return Retrieval(status=exc.code, embedding_model=embedder.model)
        cases: dict[int, list[dict[str, Any]]] = {}
        for defect, vector in zip(defects, vectors, strict=True):
            ado_id = int(defect["ado_id"])
            found = await vector_search(
                database,
                project_id=project_id,
                query_embedding=vector,
                settings=settings,
                exclude_defect_id=ado_id,
            )
            cases[ado_id] = [to_case(doc, f"C{index}") for index, doc in enumerate(found, 1)]
        return Retrieval(status="OK", cases=cases, embedding_model=embedder.model)

    return retrieve


async def reembed_entries(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    embedder: EmbeddingClient,
    *,
    project_id: str | None,
    force: bool,
    batch_size: int = 48,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> dict[str, int]:
    """Embed entries that are pending or were embedded with a different model."""
    query: dict[str, Any] = {} if project_id is None else {"project_id": project_id}
    if not force:
        query["$or"] = [
            {"status": PENDING_EMBEDDING},
            {"embedding_model": {"$ne": embedder.model}},
        ]
    entries = await database["knowledge_entries"].find(query, {"summary_text": 1}).to_list(
        length=None
    )
    done = 0
    for start in range(0, len(entries), batch_size):
        chunk = entries[start : start + batch_size]
        vectors = await embedder.embed(
            [str(entry.get("summary_text") or "") for entry in chunk], "search_document"
        )
        now = datetime.now(UTC)
        for entry, vector in zip(chunk, vectors, strict=True):
            await database["knowledge_entries"].update_one(
                {"_id": entry["_id"]},
                {
                    "$set": {
                        "embedding": vector,
                        "embedding_model": embedder.model,
                        "status": ACTIVE,
                        "updated_at": now,
                    }
                },
            )
        done += len(chunk)
        if on_progress is not None:
            await on_progress(done, len(entries))
    return {"total": len(entries), "embedded": done}
