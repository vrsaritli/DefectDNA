"""Internal API for the review commit and the knowledge base (Phase 5)."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import structlog
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Header, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from libs.common.config import settings
from libs.common.queue import MongoJobQueue
from libs.common.taxonomy import Taxonomy, TaxonomyKind
from libs.common.trends import TREND_FACT_JOB
from services.ai_knowledge_service.app.adapters.embedding import (
    EmbeddingAdapterError,
    EmbeddingClient,
    build_embedding_client,
)
from services.ai_knowledge_service.app.api import (
    _database,
    _require_gateway,
    _validate_project_id,
)
from services.ai_knowledge_service.app.knowledge import (
    ACTIVE,
    PENDING_EMBEDDING,
    REEMBED_JOB,
    defect_text,
    knowledge_text,
    remove_knowledge_entry,
    to_case,
    upsert_knowledge_entry,
    vector_search,
)
from services.ai_knowledge_service.app.repository import (
    REVIEWED_STATUSES,
    build_trend_fact,
    consume_llm_call,
    latest_by_defect,
    reviewed_trend_facts,
)

router = APIRouter()
logger = structlog.get_logger()

_CODE_FIELDS: dict[str, TaxonomyKind] = {
    "root_cause_code": "root_causes",
    "sdlc_phase_code": "sdlc_phases",
    "testing_gap_code": "testing_gaps",
}


class FinalClassification(BaseModel):
    root_cause_code: str = Field(min_length=1, max_length=64)
    sdlc_phase_code: str = Field(min_length=1, max_length=64)
    testing_gap_code: str = Field(min_length=1, max_length=64)


class DefectText(BaseModel):
    title: str = Field(default="", max_length=2_000)
    description: str = Field(default="", max_length=200_000)
    repro_steps: str = Field(default="", max_length=200_000)
    comments: str | list[dict[str, Any]] = ""
    module: str = Field(default="", max_length=500)
    release: str = Field(default="", max_length=500)


class ReviewRequest(BaseModel):
    decision: Literal["APPROVE", "CORRECT"]
    final: FinalClassification | None = None
    note: str | None = Field(default=None, max_length=2_000)
    reviewer_id: str = Field(min_length=1, max_length=64)
    reopen: bool = False
    taxonomy: Taxonomy
    defect: DefectText | None = None


class ReopenRequest(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=64)


class SeedFinal(BaseModel):
    root_cause_code: str = Field(min_length=1, max_length=64)
    sdlc_phase_code: str | None = Field(default=None, max_length=64)
    testing_gap_code: str | None = Field(default=None, max_length=64)


class SeedDefect(DefectText):
    ado_id: int = Field(ge=1)
    severity: str = Field(default="", max_length=100)
    closed_date: datetime | None = None


class SeedItem(BaseModel):
    defect: SeedDefect
    final: SeedFinal
    note: str | None = Field(default=None, max_length=2_000)


class SeedRequest(BaseModel):
    taxonomy: Taxonomy
    items: list[SeedItem] = Field(min_length=1, max_length=5_000)
    reviewer_id: str = Field(min_length=1, max_length=64)


class RetrieveRequest(BaseModel):
    text: str = Field(min_length=3, max_length=20_000)


class ReembedRequest(BaseModel):
    force: bool = False


def _reviewer(value: str) -> Any:
    return ObjectId(value) if ObjectId.is_valid(value) else value


async def _embed_documents(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    embedder: EmbeddingClient | None,
    project_id: str,
    texts: list[str],
) -> list[list[float]] | None:
    """Embed before any write. None = no provider configured (entries stay pending)."""
    if embedder is None:
        return None
    if not await consume_llm_call(database, f"embed:{project_id}", settings.embed_daily_call_cap):
        raise HTTPException(status_code=429, detail={"code": "EMBED_DAILY_CAP_REACHED"})
    try:
        return await embedder.embed(texts, "search_document")
    except EmbeddingAdapterError as exc:
        logger.warning("knowledge_embedding_failed", project_id=project_id, code=exc.code)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "EMBEDDING_PROVIDER_ERROR",
                "message": "Embedding failed; nothing was committed and the review can be retried",
            },
        ) from exc


async def _enqueue_trend(database: AsyncIOMotorDatabase[dict[str, Any]], payload: dict[str, Any]) -> str:
    return await MongoJobQueue(database).enqueue(TREND_FACT_JOB, payload)


@router.post("/internal/rcas/{rca_id}/review")
async def review_rca(
    request: Request,
    rca_id: str,
    body: ReviewRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Commit a QA Lead decision: confirmed RCA + knowledge entry + trend fact.

    The embedding is computed before any write, so a provider failure commits nothing.
    If the knowledge write fails after the RCA update, the RCA update is reverted.
    """
    _require_gateway(authorization)
    try:
        object_id = ObjectId(rca_id)
    except (InvalidId, TypeError) as exc:
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"}) from exc
    database = _database(request)
    rca = await database["rca_analyses"].find_one({"_id": object_id})
    if rca is None:
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"})
    if rca["status"] in REVIEWED_STATUSES and not body.reopen:
        raise HTTPException(status_code=409, detail={"code": "ALREADY_REVIEWED"})
    if body.taxonomy.version != rca.get("taxonomy_version"):
        raise HTTPException(status_code=409, detail={"code": "TAXONOMY_VERSION_MISMATCH"})

    proposed = rca.get("proposed") or {}
    proposed_codes = {name: proposed.get(name) for name in _CODE_FIELDS}
    if body.decision == "APPROVE":
        if not proposed:
            raise HTTPException(status_code=422, detail={"code": "NOTHING_TO_APPROVE"})
        final_codes = proposed_codes
    else:
        if body.final is None:
            raise HTTPException(status_code=422, detail={"code": "FINAL_REQUIRED"})
        final_codes = body.final.model_dump()
        for name, kind in _CODE_FIELDS.items():
            if final_codes[name] not in body.taxonomy.codes(kind):
                raise HTTPException(status_code=422, detail={"code": "UNKNOWN_TAXONOMY_CODE"})
    status = "APPROVED" if proposed and final_codes == proposed_codes else "CORRECTED"
    note = (body.note or "").strip() or None
    defect = body.defect.model_dump() if body.defect else dict(rca.get("defect_snapshot") or {})

    embedder = build_embedding_client(settings)
    text = knowledge_text(defect, final_codes, body.taxonomy, note)
    vectors = await _embed_documents(database, embedder, rca["project_id"], [text])

    now = datetime.now(UTC)
    previous = {key: rca.get(key) for key in ("status", "final", "reviewer_id", "reviewed_at", "updated_at")}
    result = await database["rca_analyses"].update_one(
        {"_id": object_id, "status": rca["status"]},
        {
            "$set": {
                "status": status,
                "final": {**final_codes, "note": note},
                "reviewer_id": _reviewer(body.reviewer_id),
                "reviewed_at": now,
                "review_source": "REVIEW",
                "updated_at": now,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail={"code": "CONCURRENT_REVIEW"})
    updated = await database["rca_analyses"].find_one({"_id": object_id})
    assert updated is not None
    try:
        entry_id = await upsert_knowledge_entry(
            database,
            rca=updated,
            defect=defect,
            final=final_codes,
            text=text,
            embedding=vectors[0] if vectors else None,
            embedding_model=embedder.model if embedder and vectors else None,
            source="REVIEW",
        )
    except Exception as exc:
        await database["rca_analyses"].update_one({"_id": object_id}, {"$set": previous})
        logger.error("knowledge_write_failed", rca_id=rca_id, error=type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "KNOWLEDGE_WRITE_FAILED"}) from exc

    fact = build_trend_fact(updated)
    trend_job_id = (
        await _enqueue_trend(database, {"project_id": updated["project_id"], "fact": fact})
        if fact
        else None
    )
    return {
        "id": rca_id,
        "project_id": updated["project_id"],
        "defect_id": updated["defect_id"],
        "status": status,
        "previous_status": rca["status"],
        "final": updated["final"],
        "reviewed_at": now,
        "trend_job_id": trend_job_id,
        "knowledge_entry_id": str(entry_id) if entry_id else None,
        "knowledge_status": ACTIVE if vectors else PENDING_EMBEDDING,
    }


@router.post("/internal/rcas/{rca_id}/reopen")
async def reopen_rca(
    request: Request,
    rca_id: str,
    body: ReopenRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Undo a review: back to the AI proposal, out of the knowledge base and trends."""
    _require_gateway(authorization)
    try:
        object_id = ObjectId(rca_id)
    except (InvalidId, TypeError) as exc:
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"}) from exc
    database = _database(request)
    rca = await database["rca_analyses"].find_one({"_id": object_id})
    if rca is None:
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"})
    if rca["status"] not in REVIEWED_STATUSES:
        raise HTTPException(status_code=409, detail={"code": "NOT_REVIEWED"})
    status = "AI_PROPOSED" if rca.get("proposed") else "NEEDS_MANUAL_RCA"
    now = datetime.now(UTC)
    result = await database["rca_analyses"].update_one(
        {"_id": object_id, "status": rca["status"]},
        {
            "$set": {
                "status": status,
                "final": None,
                "reviewer_id": None,
                "reviewed_at": None,
                "reopened_by": _reviewer(body.reviewer_id),
                "reopened_at": now,
                "updated_at": now,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail={"code": "CONCURRENT_REVIEW"})
    removed = await remove_knowledge_entry(database, rca["project_id"], rca_id)
    await _enqueue_trend(
        database,
        {
            "project_id": rca["project_id"],
            "remove": {"project_id": rca["project_id"], "defect_id": rca["defect_id"]},
        },
    )
    return {
        "id": rca_id,
        "project_id": rca["project_id"],
        "defect_id": rca["defect_id"],
        "status": status,
        "previous_status": rca["status"],
        "final": None,
        "knowledge_entries_removed": removed,
    }


@router.get("/internal/projects/{project_id}/reviewed-facts")
async def reviewed_facts(
    request: Request,
    project_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    return {"facts": await reviewed_trend_facts(_database(request), project_id)}


@router.post("/internal/projects/{project_id}/knowledge/seed")
async def seed_knowledge(
    request: Request,
    project_id: str,
    body: SeedRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Seed import: historical defects with QA-confirmed RCAs go straight to APPROVED."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    database = _database(request)
    taxonomy = body.taxonomy
    for item in body.items:
        codes = item.final.model_dump()
        for name, kind in _CODE_FIELDS.items():
            if codes[name] is not None and codes[name] not in taxonomy.codes(kind):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "UNKNOWN_TAXONOMY_CODE", "message": f"ADO-{item.defect.ado_id}"},
                )

    items = {item.defect.ado_id: item for item in body.items}
    latest = await latest_by_defect(database, project_id, list(items))
    to_seed = [item for ado_id, item in items.items() if latest.get(ado_id, {}).get("status") not in REVIEWED_STATUSES]
    skipped = len(items) - len(to_seed)
    if not to_seed:
        return {"seeded": 0, "skipped_reviewed": skipped, "knowledge_status": None}

    embedder = build_embedding_client(settings)
    texts = [
        knowledge_text(item.defect.model_dump(), item.final.model_dump(), taxonomy, item.note)
        for item in to_seed
    ]
    vectors = await _embed_documents(database, embedder, project_id, texts)

    now = datetime.now(UTC)
    queue = MongoJobQueue(database)
    for index, item in enumerate(to_seed):
        defect = item.defect.model_dump()
        current = latest.get(item.defect.ado_id)
        version = int(current["version"]) if current else 1
        final = {**item.final.model_dump(), "note": (item.note or "").strip() or None}
        closed = item.defect.closed_date
        rca = await database["rca_analyses"].find_one_and_update(
            {"project_id": project_id, "defect_id": item.defect.ado_id, "version": version},
            {
                "$set": {
                    "status": "APPROVED",
                    "final": final,
                    "review_source": "SEED_IMPORT",
                    "reviewer_id": _reviewer(body.reviewer_id),
                    "reviewed_at": now,
                    "taxonomy_version": taxonomy.version,
                    "defect_snapshot": {
                        "title": item.defect.title[:300],
                        "module": item.defect.module,
                        "release": item.defect.release,
                        "severity": item.defect.severity,
                        "closed_month": closed.strftime("%Y-%m") if closed else None,
                    },
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "proposed": None,
                    "failure_reason": None,
                    "cited_cases": [],
                    "retrieved_cases": [],
                    "model": {"llm": None, "prompt_version": None, "embedding": None},
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        assert rca is not None
        await upsert_knowledge_entry(
            database,
            rca=rca,
            defect=defect,
            final=final,
            text=texts[index],
            embedding=vectors[index] if vectors else None,
            embedding_model=embedder.model if embedder and vectors else None,
            source="SEED_IMPORT",
        )
        fact = build_trend_fact(rca)
        if fact:
            await queue.enqueue(TREND_FACT_JOB, {"project_id": project_id, "fact": fact})
    return {
        "seeded": len(to_seed),
        "skipped_reviewed": skipped,
        "knowledge_status": ACTIVE if vectors else PENDING_EMBEDDING,
    }


@router.post("/internal/projects/{project_id}/knowledge/retrieve")
async def retrieve(
    request: Request,
    project_id: str,
    body: RetrieveRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Retrieval debug: the confirmed cases a defect text would be grounded on."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    database = _database(request)
    embedder = build_embedding_client(settings)
    if embedder is None:
        raise HTTPException(status_code=503, detail={"code": "EMBEDDING_NOT_CONFIGURED"})
    if not await consume_llm_call(database, f"embed:{project_id}", settings.embed_daily_call_cap):
        raise HTTPException(status_code=429, detail={"code": "EMBED_DAILY_CAP_REACHED"})
    try:
        [vector] = await embedder.embed([defect_text({"description": body.text})], "search_query")
    except EmbeddingAdapterError as exc:
        raise HTTPException(status_code=502, detail={"code": "EMBEDDING_PROVIDER_ERROR"}) from exc
    found = await vector_search(
        database, project_id=project_id, query_embedding=vector, settings=settings
    )
    return {
        "cases": [to_case(doc, f"C{index}") for index, doc in enumerate(found, 1)],
        "top_k": settings.rag_top_k,
        "threshold": settings.rag_similarity_threshold,
    }


@router.get("/internal/projects/{project_id}/knowledge/stats")
async def knowledge_stats(
    request: Request,
    project_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    database = _database(request)
    counts: dict[str, dict[str, int]] = {"status": {}, "source": {}}
    models: dict[str, int] = {}
    async for entry in database["knowledge_entries"].find(
        {"project_id": project_id}, {"status": 1, "source": 1, "embedding_model": 1}
    ):
        for key in ("status", "source"):
            value = str(entry.get(key) or "UNKNOWN")
            counts[key][value] = counts[key].get(value, 0) + 1
        if entry.get("embedding_model"):
            models[entry["embedding_model"]] = models.get(entry["embedding_model"], 0) + 1
    return {
        "total": sum(counts["status"].values()),
        "active": counts["status"].get(ACTIVE, 0),
        "pending_embedding": counts["status"].get(PENDING_EMBEDDING, 0),
        "by_source": counts["source"],
        "embedding_models": models,
        "current_embedding_model": settings.cohere_embed_model,
        "rag_enabled": bool(settings.cohere_api_key),
    }


@router.post("/internal/projects/{project_id}/knowledge/reembed", status_code=202)
async def request_reembed(
    request: Request,
    project_id: str,
    body: ReembedRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    if not settings.cohere_api_key:
        raise HTTPException(status_code=503, detail={"code": "EMBEDDING_NOT_CONFIGURED"})
    job_id = await MongoJobQueue(_database(request)).enqueue(
        REEMBED_JOB,
        {
            "project_id": project_id,
            "force": body.force,
            "progress": {"total": 0, "done": 0, "failed": 0},
            "errors": [],
        },
    )
    return {"job_id": job_id}
