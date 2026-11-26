import asyncio
import json
import re
from collections import Counter
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from libs.common.config import settings
from libs.common.service_auth import verify_service_token
from services.ingestion_service.app.ado import (
    AdoDefectSource,
    AdoInputError,
    organization_url,
    parse_work_item_ids,
    query_id_from_link,
)
from services.ingestion_service.app.parsing import extract_confirmed_labels, parse_defect_file

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
    label: str | None = None,
    purpose: str = "IMPORT",
) -> ObjectId:
    """Record an import. ``ado_ids`` lets analysis target exactly this import's defects;
    ``purpose`` separates defect imports from knowledge ingestion."""
    now = datetime.now(UTC)
    result = await database["import_batches"].insert_one(
        {
            "project_id": project_id,
            "source": source,
            "purpose": purpose,
            "label": (label or filename or source)[:200],
            "filename": filename,
            "row_count": row_count,
            "accepted": len(defects),
            "rejected": row_count - len(defects),
            "ado_ids": [int(defect["ado_id"]) for defect in defects],
            "errors": errors,
            "by": user_id,
            "created_at": now,
        }
    )
    return cast(ObjectId, result.inserted_id)


def _pull_label(mode: str, query_link: str | None, ids: str | None, start: Any, end: Any) -> str:
    if mode == "query":
        try:
            return f"ADO saved query {query_id_from_link(query_link or '')[:8]}"
        except AdoInputError:
            return "ADO saved query"
    if mode == "ids":
        shown = ", ".join((ids or "").replace(",", " ").split()[:4])
        return f"ADO work items {shown}" + ("…" if len((ids or "").replace(",", " ").split()) > 4 else "")
    return f"ADO closed {start} to {end}"


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
        "ado_ids": [int(defect["ado_id"]) for defect in parsed["defects"]],
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
        label=f"ADO closed {start_date} to {end_date}",
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


class AdoRootCausesRequest(BaseModel):
    ado_ids: list[int] = Field(max_length=2_000)


@router.post("/internal/projects/{project_id}/defects/ado-root-causes")
async def ado_root_causes(
    request: Request,
    project_id: str,
    body: AdoRootCausesRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """The root cause recorded in ADO for each defect that has one, keyed by ADO ID."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    cursor = _database(request)["defects"].find(
        {
            "project_id": project_id,
            "ado_id": {"$in": body.ado_ids},
            "ado_root_cause": {"$nin": [None, ""]},
        },
        {"_id": 0, "ado_id": 1, "ado_root_cause": 1},
    )
    docs = await cursor.to_list(length=len(body.ado_ids) or 1)
    return {"root_causes": {str(doc["ado_id"]): doc["ado_root_cause"] for doc in docs}}


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


@router.post("/internal/seed-imports")
async def seed_import(
    request: Request,
    file: Annotated[UploadFile, File()],
    project_id: Annotated[str, Header(alias="X-Project-ID")],
    user_id: Annotated[str, Header(alias="X-User-ID")],
    mapping_json: Annotated[str | None, Form(alias="mapping")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Import defects like /internal/imports and return their QA-confirmed RCA labels."""
    imported = await import_file(
        request, file, project_id, user_id, mapping_json, authorization
    )
    await _database(request)["import_batches"].update_one(
        {"_id": ObjectId(imported["import_batch_id"])}, {"$set": {"purpose": "KNOWLEDGE"}}
    )
    await file.seek(0)
    content = await file.read(settings.max_import_file_bytes + 1)
    columns, labels = extract_confirmed_labels(
        file.filename or "", content, max_rows=settings.max_import_rows
    )
    if "root_cause" not in columns:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MISSING_RCA_COLUMNS",
                "message": "A confirmed root-cause column such as 'Root Cause (RCA)' is required",
            },
        )
    defects = await _database(request)["defects"].find(
        {"project_id": project_id, "ado_id": {"$in": sorted(labels)}}, _ANALYSIS_FIELDS
    ).to_list(length=None)
    for defect in defects:
        defect["severity"] = defect.get("severity") or ""
    return {
        **imported,
        "rca_columns": columns,
        "confirmed": [
            {"defect": defect, "labels": labels[defect["ado_id"]]} for defect in defects
        ],
    }

@router.delete("/internal/projects/{project_id}/data")
async def delete_project_data(
    request: Request,
    project_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Delete everything this service holds for a project (idempotent)."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    database = _database(request)
    defects = await database["defects"].delete_many({"project_id": project_id})
    batches = await database["import_batches"].delete_many({"project_id": project_id})
    jobs = await database["jobs"].delete_many(
        {"payload.project_id": project_id, "type": {"$in": ["import_defects", "ado_sync"]}}
    )
    return {
        "service": "ingestion-service",
        "deleted": {
            "defects": defects.deleted_count,
            "import_batches": batches.deleted_count,
            "jobs": jobs.deleted_count,
        },
    }

class AdoTarget(BaseModel):
    organization: str | None = Field(default=None, max_length=300)
    project: str | None = Field(default=None, max_length=200)


class AdoPullRequest(AdoTarget):
    mode: Literal["query", "ids", "closed"]
    query_link: str | None = Field(default=None, max_length=2_000)
    work_item_ids: str | None = Field(default=None, max_length=50_000)
    start_date: date | None = None
    end_date: date | None = None
    include_comments: bool = True


def _ado_source(target: AdoTarget) -> AdoDefectSource:
    """Source for the requested (or default) organization/project, always with the server PAT."""
    if not settings.ado_pat:
        raise HTTPException(
            status_code=503,
            detail={"code": "ADO_NOT_CONFIGURED", "message": "ADO_PAT is not set on the server"},
        )
    organization = target.organization or settings.ado_organization_url
    project = (target.project or settings.ado_project).strip()
    if not organization or not project:
        raise HTTPException(
            status_code=422,
            detail={"code": "ADO_TARGET_REQUIRED", "message": "Organization and project are required"},
        )
    try:
        return AdoDefectSource(organization_url(organization), project, settings.ado_pat)
    except AdoInputError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc


def _ado_http_error(exc: httpx.HTTPError) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code, message = {
            401: ("ADO_AUTH_FAILED", "The PAT was rejected (expired, revoked or wrong organization)"),
            403: ("ADO_FORBIDDEN", "The PAT has no read access to this project or query"),
            404: ("ADO_NOT_FOUND", "Organization, project or query not found"),
        }.get(status, ("ADO_REQUEST_FAILED", f"Azure DevOps returned HTTP {status}"))
        return HTTPException(status_code=502, detail={"code": code, "message": message})
    return HTTPException(
        status_code=502, detail={"code": "ADO_UNAVAILABLE", "message": "Azure DevOps is unreachable"}
    )


@router.post("/internal/ado/status")
async def ado_status(
    body: AdoTarget,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Connection check with the server PAT; never returns the PAT."""
    _require_gateway(authorization)
    organization = body.organization or settings.ado_organization_url
    project = body.project or settings.ado_project
    result: dict[str, Any] = {
        "organization": organization,
        "project": project,
        "pat_configured": bool(settings.ado_pat),
        "connected": False,
    }
    try:
        source = _ado_source(body)
    except HTTPException as exc:
        setup: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        return {**result, "error": setup.get("code"), "message": setup.get("message")}
    try:
        info = await source.check_connection()
        result.update(
            connected=True, organization=source.organization_url, project=info["project_name"]
        )
    except httpx.HTTPError as exc:
        failure: dict[str, Any] = cast(dict[str, Any], _ado_http_error(exc).detail)
        result.update(error=failure["code"], message=failure["message"])
    finally:
        await source.close()
    return result


@router.post("/internal/ado-pull")
async def ado_pull(
    request: Request,
    body: AdoPullRequest,
    project_id: Annotated[str, Header(alias="X-Project-ID")],
    user_id: Annotated[str, Header(alias="X-User-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Pull defects from a saved query link, a list of work item IDs, or closed-by-date."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    source = _ado_source(body)
    try:
        # The project's RCA field, when it has one, is kept as ado_root_cause.
        rca_ref = None
        for candidate in RCA_FIELD_CANDIDATES:
            rca_ref = await source.find_field(candidate)
            if rca_ref:
                break
        extra = (rca_ref,) if rca_ref else ()
        if body.mode == "query":
            defects = await source.fetch_saved_query(
                query_id_from_link(body.query_link or ""),
                include_comments=body.include_comments,
                extra_fields=extra,
            )
        elif body.mode == "ids":
            defects = await source.fetch_by_ids(
                parse_work_item_ids(body.work_item_ids or ""),
                include_comments=body.include_comments,
                extra_fields=extra,
            )
        else:
            if body.start_date is None or body.end_date is None or body.start_date > body.end_date:
                raise HTTPException(status_code=422, detail={"code": "INVALID_DATE_RANGE"})
            defects = await source.fetch_closed_defects(
                body.start_date,
                body.end_date,
                include_comments=body.include_comments,
                extra_fields=extra,
            )
    except AdoInputError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except httpx.HTTPError as exc:
        raise _ado_http_error(exc) from exc
    finally:
        await source.close()

    for defect in defects:
        value = (defect.pop("_extra", {}) or {}).get(rca_ref) if rca_ref else None
        if value is not None and str(value).strip():
            defect["ado_root_cause"] = str(value).strip()

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
        label=_pull_label(body.mode, body.query_link, body.work_item_ids, body.start_date, body.end_date),
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
        "mode": body.mode,
        "rows_total": len(defects),
        "upserted": upserted,
        "ado_ids": [defect["ado_id"] for defect in defects],
        "comments": sum(len(d["comments"]) for d in defects if isinstance(d.get("comments"), list)),
        "states": dict(Counter(str(defect.get("state") or "Unknown") for defect in defects)),
    }

RCA_FIELD_CANDIDATES = ("Root Cause (RCA)", "RCA", "Root Cause")


class AdoSeedPullRequest(AdoPullRequest):
    rca_field: str | None = Field(default=None, max_length=200)


@router.post("/internal/ado-seed-pull")
async def ado_seed_pull(
    request: Request,
    body: AdoSeedPullRequest,
    project_id: Annotated[str, Header(alias="X-Project-ID")],
    user_id: Annotated[str, Header(alias="X-User-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Pull defects from ADO with their confirmed root cause (the RCA field) for the knowledge base."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    source = _ado_source(body)
    try:
        candidates = (body.rca_field,) if body.rca_field else RCA_FIELD_CANDIDATES
        rca_ref = None
        for candidate in candidates:
            rca_ref = await source.find_field(candidate)
            if rca_ref:
                break
        if rca_ref is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ADO_RCA_FIELD_NOT_FOUND",
                    "message": f"No ADO field named {' / '.join(candidates)} in this project",
                },
            )
        extra = (rca_ref,)
        if body.mode == "query":
            defects = await source.fetch_saved_query(
                query_id_from_link(body.query_link or ""),
                include_comments=body.include_comments,
                extra_fields=extra,
            )
        elif body.mode == "ids":
            defects = await source.fetch_by_ids(
                parse_work_item_ids(body.work_item_ids or ""),
                include_comments=body.include_comments,
                extra_fields=extra,
            )
        else:
            if body.start_date is None or body.end_date is None or body.start_date > body.end_date:
                raise HTTPException(status_code=422, detail={"code": "INVALID_DATE_RANGE"})
            defects = await source.fetch_closed_defects(
                body.start_date,
                body.end_date,
                include_comments=body.include_comments,
                extra_fields=extra,
            )
    except AdoInputError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except httpx.HTTPError as exc:
        raise _ado_http_error(exc) from exc
    finally:
        await source.close()

    confirmed = []
    for defect in defects:
        value = (defect.pop("_extra", {}) or {}).get(rca_ref)
        label = str(value).strip() if value is not None else ""
        if label:
            defect["ado_root_cause"] = label
            confirmed.append({"defect": dict(defect), "labels": {"root_cause": label}})

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
        label=_pull_label(body.mode, body.query_link, body.work_item_ids, body.start_date, body.end_date),
        purpose="KNOWLEDGE",
    )
    upserted = await _upsert_defects(database, project_id, batch_id, defects)
    await _create_completed_job(
        database,
        project_id=project_id,
        job_type="ado_sync",
        batch_id=batch_id,
        total=len(defects),
        succeeded=len(defects),
        errors=[],
    )
    return {
        "import_batch_id": str(batch_id),
        "preview": {"rows_total": len(defects), "rows_valid": len(defects), "row_errors": []},
        "upserted": upserted,
        "rca_columns": {"root_cause": rca_ref},
        "confirmed": confirmed,
    }

def _batch_view(batch: dict[str, Any]) -> dict[str, Any]:
    created = batch.get("created_at")
    return {
        "id": str(batch["_id"]),
        "source": batch.get("source"),
        "label": batch.get("label") or batch.get("filename") or batch.get("source"),
        "filename": batch.get("filename"),
        "defects": len(batch.get("ado_ids") or []) or int(batch.get("accepted") or 0),
        "rows": int(batch.get("row_count") or 0),
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
    }


@router.get("/internal/projects/{project_id}/import-batches")
async def list_import_batches(
    request: Request,
    project_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Recent defect imports (Excel uploads and ADO pulls), newest first; knowledge ingestion excluded."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    cursor = (
        _database(request)["import_batches"]
        .find({"project_id": project_id, "purpose": {"$ne": "KNOWLEDGE"}, "accepted": {"$gt": 0}})
        .sort("created_at", -1)
        .limit(limit)
    )
    return {"batches": [_batch_view(batch) for batch in await cursor.to_list(length=limit)]}


@router.get("/internal/projects/{project_id}/import-batches/{batch_id}")
async def get_import_batch(
    request: Request,
    project_id: str,
    batch_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """One import with the IDs of the defects it brought in."""
    _require_gateway(authorization)
    _validate_project_id(project_id)
    try:
        object_id = ObjectId(batch_id)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail={"code": "IMPORT_NOT_FOUND"}) from exc
    database = _database(request)
    batch = await database["import_batches"].find_one({"_id": object_id, "project_id": project_id})
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "IMPORT_NOT_FOUND"})
    ado_ids = batch.get("ado_ids")
    if not ado_ids:  # imports recorded before ado_ids existed
        ado_ids = [
            doc["ado_id"]
            async for doc in database["defects"].find(
                {"project_id": project_id, "import_batch_id": object_id}, {"ado_id": 1}
            )
        ]
    return {**_batch_view(batch), "ado_ids": sorted(int(item) for item in ado_ids)}