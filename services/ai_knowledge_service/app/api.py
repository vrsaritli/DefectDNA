"""Internal (service-token only) API of the ai-knowledge-service."""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Header, HTTPException, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from libs.common.config import settings
from libs.common.queue import MongoJobQueue
from libs.common.service_auth import verify_service_token
from libs.common.taxonomy import Taxonomy
from services.ai_knowledge_service.app.repository import (
    RCA_STATUSES,
    REVIEWED_STATUSES,
    find_latest_for_defect,
    latest_by_defect,
    serialize_rca,
)
from services.ai_knowledge_service.app.worker import JOB_TYPE

router = APIRouter()
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


class DefectForAnalysis(BaseModel):
    ado_id: int = Field(ge=1)
    title: str = Field(max_length=2_000)
    description: str = Field(default="", max_length=200_000)
    repro_steps: str = Field(default="", max_length=200_000)
    comments: str | list[dict[str, Any]] = ""
    module: str = Field(default="", max_length=500)
    release: str = Field(default="", max_length=500)
    severity: str = Field(default="", max_length=100)
    priority: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=100)
    closed_date: datetime | None = None


class AnalysisJobRequest(BaseModel):
    taxonomy: Taxonomy
    defects: list[DefectForAnalysis] = Field(min_length=1)
    requested_by: str = Field(min_length=1, max_length=64)
    reanalyze: bool = False
    correlation_id: str | None = Field(default=None, max_length=128)


def _require_gateway(authorization: str | None) -> None:
    if len(settings.service_token_secret.encode("utf-8")) < 32:
        raise HTTPException(status_code=503, detail={"code": "SERVICE_AUTH_NOT_CONFIGURED"})
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    payload = verify_service_token(token, settings.service_token_secret)
    if payload is None or payload.get("service") != "api-gateway":
        raise HTTPException(status_code=401, detail={"code": "INVALID_SERVICE_TOKEN"})


def _database(request: Request) -> AsyncIOMotorDatabase[dict[str, Any]]:
    client = getattr(request.app.state, "mongo_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail={"code": "DATABASE_UNAVAILABLE"})
    return cast(AsyncIOMotorDatabase[dict[str, Any]], client[settings.mongodb_db])


def _validate_project_id(project_id: str) -> None:
    if not _PROJECT_ID_PATTERN.fullmatch(project_id):
        raise HTTPException(status_code=422, detail={"code": "INVALID_PROJECT_ID"})


@router.post("/internal/projects/{project_id}/analyses", status_code=202)
async def create_analysis_job(
    request: Request,
    project_id: str,
    body: AnalysisJobRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    if len(body.defects) > settings.max_analysis_batch:
        raise HTTPException(status_code=422, detail={"code": "BATCH_TOO_LARGE"})
    if not settings.groq_api_key:
        raise HTTPException(status_code=503, detail={"code": "LLM_NOT_CONFIGURED"})

    database = _database(request)
    unique: dict[int, DefectForAnalysis] = {defect.ado_id: defect for defect in body.defects}
    existing = await latest_by_defect(database, project_id, list(unique))
    queued: list[dict[str, Any]] = []
    skipped_reviewed = 0
    skipped_analyzed = 0
    for ado_id, defect in unique.items():
        latest = existing.get(ado_id)
        if latest is not None and latest["status"] in REVIEWED_STATUSES:
            skipped_reviewed += 1
        elif latest is not None and not body.reanalyze:
            skipped_analyzed += 1
        else:
            queued.append(defect.model_dump(mode="python"))

    response: dict[str, Any] = {
        "job_id": None,
        "queued": len(queued),
        "skipped_reviewed": skipped_reviewed,
        "skipped_already_analyzed": skipped_analyzed,
        "taxonomy_version": body.taxonomy.version,
    }
    if not queued:
        return response

    job_id = await MongoJobQueue(database).enqueue(
        JOB_TYPE,
        {
            "project_id": project_id,
            "taxonomy": body.taxonomy.to_payload(),
            "defects": queued,
            "requested_by": body.requested_by,
            "correlation_id": body.correlation_id,
            "requested_at": datetime.now(UTC),
            "progress": {
                "total": len(queued),
                "done": 0,
                "failed": 0,
                "proposed": 0,
                "needs_manual": 0,
                "skipped": 0,
            },
            "errors": [],
        },
    )
    response["job_id"] = job_id
    return response


@router.get("/internal/projects/{project_id}/analysis-state")
async def analysis_state(
    request: Request,
    project_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Defect IDs that already have an RCA, so callers can select unanalyzed defects."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    latest = await latest_by_defect(_database(request), project_id)
    return {
        "analyzed": sorted(latest),
        "reviewed": sorted(
            defect_id
            for defect_id, doc in latest.items()
            if doc["status"] in REVIEWED_STATUSES
        ),
    }


@router.get("/internal/projects/{project_id}/rcas")
async def list_rcas(
    request: Request,
    project_id: str,
    authorization: Annotated[str | None, Header()] = None,
    status: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    query: dict[str, Any] = {"project_id": project_id}
    if status:
        if status not in RCA_STATUSES:
            raise HTTPException(status_code=422, detail={"code": "INVALID_STATUS"})
        query["status"] = status
    collection = _database(request)["rca_analyses"]
    total = await collection.count_documents(query)
    cursor = (
        collection.find(query)
        .sort([("updated_at", -1), ("defect_id", -1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    rcas = [serialize_rca(doc) for doc in await cursor.to_list(length=page_size)]
    return {"page": page, "page_size": page_size, "total": total, "rcas": rcas}


@router.get("/internal/rcas/{rca_id}")
async def get_rca(
    request: Request,
    rca_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    try:
        object_id = ObjectId(rca_id)
    except (InvalidId, TypeError) as exc:
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"}) from exc
    doc = await _database(request)["rca_analyses"].find_one({"_id": object_id})
    if doc is None:
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"})
    return {"rca": serialize_rca(doc)}


@router.get("/internal/projects/{project_id}/defects/{defect_id}/rca")
async def get_defect_rca(
    request: Request,
    project_id: str,
    defect_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    doc = await find_latest_for_defect(_database(request), project_id, defect_id)
    return {"rca": serialize_rca(doc) if doc else None}
