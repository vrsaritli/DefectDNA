import json
import asyncio
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, cast

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from libs.common.config import settings
from libs.common.service_auth import verify_service_token
from services.ingestion_service.app.ado import AdoDefectSource
from services.ingestion_service.app.parsing import parse_defect_file

router = APIRouter()
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


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


def _validate_user_id(user_id: str) -> ObjectId:
    try:
        return ObjectId(user_id)
    except InvalidId as exc:
        raise HTTPException(status_code=401, detail={"code": "INVALID_SERVICE_CONTEXT"}) from exc


def _serialize_defect(defect: dict[str, Any]) -> dict[str, Any]:
    defect["id"] = str(defect.pop("_id"))
    if isinstance(defect.get("import_batch_id"), ObjectId):
        defect["import_batch_id"] = str(defect["import_batch_id"])
    return defect


async def _upsert_defects(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    batch_id: ObjectId,
    defects: list[dict[str, Any]],
) -> int:
    if not defects:
        return 0
    now = datetime.now(UTC)

    async def upsert(defect: dict[str, Any]) -> int:
        result = await database["defects"].update_one(
            {"project_id": project_id, "ado_id": defect["ado_id"]},
            {
                "$set": {
                    **defect,
                    "project_id": project_id,
                    "import_batch_id": batch_id,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return 1 if result.upserted_id is not None or result.modified_count > 0 else 0

    return sum(await asyncio.gather(*(upsert(defect) for defect in defects)))


async def _create_batch(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    project_id: str,
    source: str,
    filename: str | None,
    row_count: int,
    defects: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    user_id: ObjectId,
) -> ObjectId:
    now = datetime.now(UTC)
    result = await database["import_batches"].insert_one(
        {
            "project_id": project_id,
            "source": source,
            "filename": filename,
            "row_count": row_count,
            "accepted": len(defects),
            "rejected": row_count - len(defects),
            "errors": errors,
            "by": user_id,
            "created_at": now,
        }
    )
    return cast(ObjectId, result.inserted_id)


async def _create_completed_job(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    project_id: str,
    job_type: str,
    batch_id: ObjectId,
    total: int,
    succeeded: int,
    errors: list[dict[str, Any]],
) -> ObjectId:
    now = datetime.now(UTC)
    result = await database["jobs"].insert_one(
        {
            "type": job_type,
            "payload": {
                "project_id": project_id,
                "import_batch_id": str(batch_id),
                "progress": {
                    "total": total,
                    "done": succeeded,
                    "failed": total - succeeded,
                },
                "errors": errors,
            },
            "status": "SUCCEEDED",
            "attempts": 1,
            "owner": "ingestion-service",
            "lease_expires_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    return cast(ObjectId, result.inserted_id)


@router.post("/internal/imports")
async def import_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    project_id: Annotated[str, Header(alias="X-Project-ID")],
    user_id: Annotated[str, Header(alias="X-User-ID")],
    mapping_json: Annotated[str | None, Form(alias="mapping")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    content = await file.read(settings.max_import_file_bytes + 1)
    if len(content) > settings.max_import_file_bytes:
        raise HTTPException(status_code=413, detail={"code": "FILE_TOO_LARGE"})
    try:
        mapping = json.loads(mapping_json) if mapping_json else None
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_COLUMN_MAPPING"}) from exc
    if mapping is not None and (
        not isinstance(mapping, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in mapping.items())
    ):
        raise HTTPException(status_code=422, detail={"code": "INVALID_COLUMN_MAPPING"})

    parsed = parse_defect_file(
        file.filename or "",
        content,
        mapping=mapping,
        max_rows=settings.max_import_rows,
    )
    database = _database(request)
    batch_id = await _create_batch(
        database,
        project_id=project_id,
        source="CSV_IMPORT",
        filename=file.filename,
        row_count=parsed["rows_total"],
        defects=parsed["defects"],
        errors=parsed["row_errors"],
        user_id=_validate_user_id(user_id),
    )
    upserted = await _upsert_defects(database, project_id, batch_id, parsed["defects"])
    job_id = await _create_completed_job(
        database,
        project_id=project_id,
        job_type="import_defects",
        batch_id=batch_id,
        total=parsed["rows_total"],
        succeeded=parsed["rows_valid"],
        errors=parsed["row_errors"],
    )
    return {
        "job_id": str(job_id),
        "import_batch_id": str(batch_id),
        "upserted": upserted,
        "preview": {
            "detected_columns": parsed["detected_columns"],
            "mapped": parsed["mapped"],
            "rows_total": parsed["rows_total"],
            "rows_valid": parsed["rows_valid"],
            "row_errors": parsed["row_errors"],
            "rows": parsed["preview_rows"],
        },
    }


@router.post("/internal/ado-sync")
async def sync_ado(
    request: Request,
    project_id: Annotated[str, Header(alias="X-Project-ID")],
    start_date: date,
    end_date: date,
    user_id: Annotated[str, Header(alias="X-User-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    if start_date > end_date:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATE_RANGE"})
    if not settings.ado_organization_url or not settings.ado_project or not settings.ado_pat:
        raise HTTPException(status_code=503, detail={"code": "ADO_NOT_CONFIGURED"})

    try:
        source = AdoDefectSource(
            settings.ado_organization_url,
            settings.ado_project,
            settings.ado_pat,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ADO_CONFIGURATION_INVALID"},
        ) from exc
    try:
        defects = await source.fetch_closed_defects(start_date, end_date)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail={"code": "ADO_REQUEST_FAILED"}) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail={"code": "ADO_UNAVAILABLE"}) from exc
    finally:
        await source.close()

    database = _database(request)
    batch_id = await _create_batch(
        database,
        project_id=project_id,
        source="ADO_API",
        filename=None,
        row_count=len(defects),
        defects=defects,
        errors=[],
        user_id=_validate_user_id(user_id),
    )
    upserted = await _upsert_defects(database, project_id, batch_id, defects)
    job_id = await _create_completed_job(
        database,
        project_id=project_id,
        job_type="ado_sync",
        batch_id=batch_id,
        total=len(defects),
        succeeded=len(defects),
        errors=[],
    )
    return {
        "job_id": str(job_id),
        "import_batch_id": str(batch_id),
        "rows_total": len(defects),
        "upserted": upserted,
    }


@router.get("/internal/projects/{project_id}/defects")
async def list_defects(
    request: Request,
    project_id: str,
    authorization: Annotated[str | None, Header()] = None,
    module: str | None = None,
    release: str | None = None,
    state: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    query: dict[str, Any] = {"project_id": project_id}
    for field, value in (("module", module), ("release", release), ("state", state)):
        if value:
            query[field] = value
    collection = _database(request)["defects"]
    total = await collection.count_documents(query)
    cursor = (
        collection.find(query)
        .sort([("closed_date", -1), ("ado_id", -1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    defects = [_serialize_defect(doc) for doc in await cursor.to_list(length=page_size)]
    return {"page": page, "page_size": page_size, "total": total, "defects": defects}


_ANALYZABLE_STATES = ("Closed", "Resolved", "Done")
_ANALYSIS_FIELDS = {
    "_id": 0,
    "ado_id": 1,
    "title": 1,
    "description": 1,
    "repro_steps": 1,
    "comments": 1,
    "module": 1,
    "release": 1,
    "severity": 1,
    "priority": 1,
    "state": 1,
    "tags": 1,
    "closed_date": 1,
}


class DefectsForAnalysisRequest(BaseModel):
    ado_ids: list[int] | None = Field(default=None, max_length=1_000)
    exclude_ids: list[int] = Field(default_factory=list, max_length=100_000)
    limit: int = Field(default=200, ge=1, le=1_000)


@router.post("/internal/projects/{project_id}/defects/for-analysis")
async def defects_for_analysis(
    request: Request,
    project_id: str,
    body: DefectsForAnalysisRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Defect fields the RCA pipeline needs.

    With explicit ``ado_ids`` any state is allowed; otherwise only closed/resolved
    defects are selected (RCA applies to closed defects), minus ``exclude_ids``.
    """
    _require_gateway(authorization)
    _validate_project_id(project_id)
    query: dict[str, Any] = {"project_id": project_id}
    if body.ado_ids is not None:
        query["ado_id"] = {"$in": body.ado_ids}
    else:
        query["state"] = {"$in": list(_ANALYZABLE_STATES)}
        if body.exclude_ids:
            query["ado_id"] = {"$nin": body.exclude_ids}
    collection = _database(request)["defects"]
    total = await collection.count_documents(query)
    cursor = (
        collection.find(query, _ANALYSIS_FIELDS)
        .sort([("closed_date", -1), ("ado_id", -1)])
        .limit(body.limit)
    )
    defects = await cursor.to_list(length=body.limit)
    return {"defects": defects, "total_matching": total}


@router.get("/internal/projects/{project_id}/defects/{ado_id}")
async def get_defect(
    request: Request,
    project_id: str,
    ado_id: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    defect = await _database(request)["defects"].find_one(
        {"project_id": project_id, "ado_id": ado_id}
    )
    if defect is None:
        raise HTTPException(status_code=404, detail={"code": "DEFECT_NOT_FOUND"})
    return {"defect": _serialize_defect(defect), "rca": None}
