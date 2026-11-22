"""Phase 4 gateway routes: taxonomy, analysis jobs and read-only RCA views."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from libs.common.config import settings
from libs.common.taxonomy import Taxonomy, TaxonomyLists
from services.api_gateway.app import api as core
from services.api_gateway.app.auth.audit import write_audit
from services.api_gateway.app.auth.dependencies import (
    CurrentUserDependency,
    DatabaseDependency,
)
from services.api_gateway.app.taxonomy import (
    TaxonomyConflict,
    enrich_rca,
    get_current_taxonomy,
    save_taxonomy_version,
)

router = APIRouter(prefix="/api/v1")

_READERS = {"VIEWER", "QA_LEAD", "ADMIN"}
_ANALYSTS = {"QA_LEAD", "ADMIN"}
_RCA_STATUSES = {"AI_PROPOSED", "NEEDS_MANUAL_RCA", "APPROVED", "CORRECTED"}


class AnalysisRequest(BaseModel):
    ado_ids: list[int] | None = Field(default=None, min_length=1, max_length=1_000)
    reanalyze: bool = False


class ProjectSettingsRequest(BaseModel):
    llm_analysis_enabled: bool


def _taxonomy_view(project_id: str, taxonomy: Taxonomy) -> dict[str, Any]:
    return {"project_id": project_id, **taxonomy.model_dump(mode="json")}


async def _llm_analysis_enabled(database: Any, project_id: str) -> bool:
    project = await database["projects"].find_one({"_id": project_id})
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    value = (project.get("settings") or {}).get("llm_analysis_enabled")
    if isinstance(value, bool):
        return value
    # Data-handling gate (NFR-05): production projects start disabled until an Admin
    # enables them; dev/test defaults to enabled for sanitized data.
    return settings.env.strip().casefold() not in {"prod", "production"}


@router.get("/projects/{project_id}/taxonomy")
async def get_taxonomy(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _READERS)
    return _taxonomy_view(project_id, await get_current_taxonomy(database, project_id))


@router.put("/projects/{project_id}/taxonomy")
async def update_taxonomy(
    project_id: str,
    body: TaxonomyLists,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _ANALYSTS)
    try:
        taxonomy = await save_taxonomy_version(
            database, project_id, body, current["user"]["_id"]
        )
    except TaxonomyConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "TAXONOMY_VERSION_CONFLICT"}) from exc
    await write_audit(
        database,
        action="TAXONOMY_UPDATED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="taxonomies",
        entity_id=taxonomy.version,
        detail={
            "version": taxonomy.version,
            "counts": {
                "root_causes": len(taxonomy.root_causes),
                "sdlc_phases": len(taxonomy.sdlc_phases),
                "testing_gaps": len(taxonomy.testing_gaps),
            },
        },
    )
    return _taxonomy_view(project_id, taxonomy)


@router.get("/projects/{project_id}/settings")
async def get_project_settings(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _READERS)
    return {
        "project_id": project_id,
        "llm_analysis_enabled": await _llm_analysis_enabled(database, project_id),
    }


@router.put("/projects/{project_id}/settings")
async def update_project_settings(
    project_id: str,
    body: ProjectSettingsRequest,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, {"ADMIN"})
    result = await database["projects"].update_one(
        {"_id": project_id},
        {
            "$set": {
                "settings.llm_analysis_enabled": body.llm_analysis_enabled,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    await write_audit(
        database,
        action="PROJECT_SETTINGS_CHANGED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="projects",
        entity_id=project_id,
        detail={"llm_analysis_enabled": body.llm_analysis_enabled},
    )
    return {"project_id": project_id, "llm_analysis_enabled": body.llm_analysis_enabled}


@router.post("/projects/{project_id}/analyses", status_code=202)
async def request_analysis(
    request: Request,
    project_id: str,
    body: AnalysisRequest,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _ANALYSTS)
    if not await _llm_analysis_enabled(database, project_id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LLM_ANALYSIS_DISABLED",
                "message": "AI analysis is disabled for this project until an Admin enables it",
            },
        )
    limit = settings.max_analysis_batch
    if body.ado_ids is not None and len(set(body.ado_ids)) > limit:
        raise HTTPException(status_code=422, detail={"code": "BATCH_TOO_LARGE"})

    exclude_ids: list[int] = []
    if body.ado_ids is None:
        _, state = await core._call_ai_knowledge(
            "GET", f"/internal/projects/{project_id}/analysis-state"
        )
        key = "reviewed" if body.reanalyze else "analyzed"
        exclude_ids = [int(item) for item in state.get(key, [])]

    _, selection = await core._call_ingestion(
        "POST",
        f"/internal/projects/{project_id}/defects/for-analysis",
        json_body={
            "ado_ids": sorted(set(body.ado_ids)) if body.ado_ids is not None else None,
            "exclude_ids": exclude_ids,
            "limit": limit,
        },
    )
    defects = selection.get("defects", [])
    total_matching = int(selection.get("total_matching", len(defects)))
    taxonomy = await get_current_taxonomy(database, project_id)
    if not defects:
        return {
            "job_id": None,
            "queued": 0,
            "skipped_reviewed": 0,
            "skipped_already_analyzed": 0,
            "remaining": 0,
            "taxonomy_version": taxonomy.version,
        }

    correlation_id = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex
    _, result = await core._call_ai_knowledge(
        "POST",
        f"/internal/projects/{project_id}/analyses",
        json_body={
            "taxonomy": taxonomy.to_payload(),
            "defects": defects,
            "requested_by": str(current["user"]["_id"]),
            "reanalyze": body.reanalyze,
            "correlation_id": correlation_id[:128],
        },
    )
    result["remaining"] = max(0, total_matching - len(defects))
    await write_audit(
        database,
        action="ANALYSIS_REQUESTED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="jobs",
        entity_id=result.get("job_id"),
        detail={
            "queued": result.get("queued", 0),
            "skipped_reviewed": result.get("skipped_reviewed", 0),
            "skipped_already_analyzed": result.get("skipped_already_analyzed", 0),
            "taxonomy_version": taxonomy.version,
            "reanalyze": body.reanalyze,
        },
    )
    return result


@router.get("/projects/{project_id}/rcas")
async def list_rcas(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    status: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _READERS)
    if status is not None and status not in _RCA_STATUSES:
        raise HTTPException(status_code=422, detail={"code": "INVALID_STATUS"})
    _, result = await core._call_ai_knowledge(
        "GET",
        f"/internal/projects/{project_id}/rcas",
        params={"page": page, "page_size": page_size, **({"status": status} if status else {})},
    )
    cache: dict[int, Taxonomy | None] = {}
    rcas = [await enrich_rca(database, project_id, rca, cache) for rca in result.get("rcas", [])]
    return {
        "page": result.get("page", page),
        "page_size": result.get("page_size", page_size),
        "total": result.get("total", len(rcas)),
        "rcas": rcas,
    }


@router.get("/rcas/{rca_id}")
async def get_rca(
    rca_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    _, result = await core._call_ai_knowledge("GET", f"/internal/rcas/{rca_id}")
    rca = result.get("rca")
    if not isinstance(rca, dict) or not isinstance(rca.get("project_id"), str):
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"})
    project_id: str = rca["project_id"]
    await core._require_project_role(project_id, current, database, _READERS)
    return await enrich_rca(database, project_id, rca)
