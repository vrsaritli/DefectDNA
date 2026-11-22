"""Phase 5 gateway routes: knowledge-base seed import, retrieval debug, stats, re-embed."""

import re
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from libs.common.config import settings
from libs.common.taxonomy import Taxonomy, TaxonomyKind
from services.api_gateway.app import api as core
from services.api_gateway.app.auth.audit import write_audit
from services.api_gateway.app.auth.dependencies import (
    CurrentUserDependency,
    DatabaseDependency,
)
from services.api_gateway.app.auth.rate_limit import enforce_quota
from services.api_gateway.app.taxonomy import get_current_taxonomy

router = APIRouter(prefix="/api/v1")

_READERS = {"VIEWER", "QA_LEAD", "ADMIN"}
_ANALYSTS = {"QA_LEAD", "ADMIN"}
_LABEL_KINDS: dict[str, TaxonomyKind] = {
    "root_cause": "root_causes",
    "sdlc_phase": "sdlc_phases",
    "testing_gap": "testing_gaps",
}
_CODE_KEYS = {
    "root_cause": "root_cause_code",
    "sdlc_phase": "sdlc_phase_code",
    "testing_gap": "testing_gap_code",
}


class RetrieveBody(BaseModel):
    text: str = Field(min_length=3, max_length=20_000)


class ReembedBody(BaseModel):
    force: bool = False


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def map_label(taxonomy: Taxonomy, kind: TaxonomyKind, value: str) -> str | None:
    """Match a free-text RCA label to a taxonomy code by code or label.

    "Third Party" -> THIRD_PARTY, "test gap" -> TEST_GAP; legacy sub-categories such as
    "Code - Front-end" fall back to their parent ("Code").
    """
    candidates = [value, value.split(" - ")[0], value.split("/")[0]]
    lookup = {}
    for item in taxonomy.items(kind):
        lookup[_normalise(item.code)] = item.code
        lookup[_normalise(item.label)] = item.code
    for candidate in candidates:
        code = lookup.get(_normalise(candidate))
        if code:
            return code
    return None


@router.post("/projects/{project_id}/knowledge/seed")
async def seed_knowledge(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    file: UploadFile = File(...),  # noqa: B008 - FastAPI form declaration
    mapping: str | None = Form(default=None),
) -> dict[str, Any]:
    """Knowledge ingestion from Excel/CSV: defects with a confirmed RCA column (Admin only)."""
    await _require_ingestion_admin(project_id, current, database)
    content = await file.read(settings.max_import_file_bytes + 1)
    if len(content) > settings.max_import_file_bytes:
        raise HTTPException(status_code=413, detail={"code": "FILE_TOO_LARGE"})
    _, imported = await core._call_ingestion(
        "POST",
        "/internal/seed-imports",
        headers={"X-Project-ID": project_id, "X-User-ID": str(current["user"]["_id"])},
        files={
            "file": (
                file.filename or "",
                content,
                file.content_type or "application/octet-stream",
            )
        },
        data={"mapping": mapping} if mapping is not None else None,
    )
    return await _seed_confirmed(database, project_id, current, imported, source="EXCEL")


class AdoIngestBody(BaseModel):
    mode: Literal["query", "ids", "closed"] = "query"
    query_link: str | None = Field(default=None, max_length=2_000)
    work_item_ids: str | None = Field(default=None, max_length=50_000)
    start_date: date | None = None
    end_date: date | None = None
    include_comments: bool = True
    rca_field: str | None = Field(default=None, max_length=200)


@router.post("/projects/{project_id}/knowledge/seed-ado")
async def seed_knowledge_from_ado(
    project_id: str,
    body: AdoIngestBody,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Knowledge ingestion from an ADO query, reading the confirmed root cause field (Admin only)."""
    await _require_ingestion_admin(project_id, current, database)
    saved = (await database["projects"].find_one({"_id": project_id}, {"ado": 1}) or {}).get("ado") or {}
    _, imported = await core._call_ingestion(
        "POST",
        "/internal/ado-seed-pull",
        headers={"X-Project-ID": project_id, "X-User-ID": str(current["user"]["_id"])},
        json_body={
            **body.model_dump(mode="json"),
            "organization": saved.get("organization"),
            "project": saved.get("project"),
        },
    )
    return await _seed_confirmed(database, project_id, current, imported, source="ADO_QUERY")


async def _require_ingestion_admin(project_id: str, current: Any, database: Any) -> None:
    # Ingested RCAs become APPROVED knowledge without review, so only project Admins may run it.
    await core._require_project_role(project_id, current, database, {"ADMIN"})
    await enforce_quota(
        database,
        scope=f"upload:{current['user']['_id']}",
        limit=settings.upload_quota_per_hour,
        window_seconds=3600,
    )


async def _seed_confirmed(
    database: Any, project_id: str, current: Any, imported: dict[str, Any], *, source: str
) -> dict[str, Any]:
    """Map confirmed labels to taxonomy codes and write them to the knowledge base."""
    taxonomy = await get_current_taxonomy(database, project_id)
    items: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for entry in imported.get("confirmed", []):
        defect = entry.get("defect") or {}
        labels = entry.get("labels") or {}
        final: dict[str, Any] = {}
        problems = []
        for name, kind in _LABEL_KINDS.items():
            raw = labels.get(name)
            if not raw:
                final[_CODE_KEYS[name]] = None
                continue
            code = map_label(taxonomy, kind, str(raw))
            if code is None:
                problems.append({"field": name, "value": str(raw)[:80]})
            final[_CODE_KEYS[name]] = code
        if final["root_cause_code"] is None:
            unmapped.append({"ado_id": defect.get("ado_id"), "issues": problems})
            continue
        if problems:
            unmapped.append({"ado_id": defect.get("ado_id"), "issues": problems})
        items.append({"defect": defect, "final": final})

    seeded: dict[str, Any] = {"seeded": 0, "skipped_reviewed": 0, "knowledge_status": None}
    if items:
        _, seeded = await core._call_ai_knowledge(
            "POST",
            f"/internal/projects/{project_id}/knowledge/seed",
            json_body={
                "taxonomy": taxonomy.to_payload(),
                "items": items,
                "reviewer_id": str(current["user"]["_id"]),
            },
        )
    preview = imported.get("preview", {})
    await write_audit(
        database,
        action="KNOWLEDGE_SEEDED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="knowledge_entries",
        entity_id=imported.get("import_batch_id"),
        detail={
            "source": source,
            "rows_total": preview.get("rows_total", 0),
            "confirmed_rows": len(imported.get("confirmed", [])),
            "seeded": seeded.get("seeded", 0),
            "unmapped": len(unmapped),
        },
    )
    return {
        "import_batch_id": imported.get("import_batch_id"),
        "rows_total": preview.get("rows_total", 0),
        "rows_valid": preview.get("rows_valid", 0),
        "row_errors": preview.get("row_errors", []),
        "rca_columns": imported.get("rca_columns", {}),
        "confirmed_rows": len(imported.get("confirmed", [])),
        "seeded": seeded.get("seeded", 0),
        "skipped_reviewed": seeded.get("skipped_reviewed", 0),
        "already_confirmed_ids": seeded.get("skipped_reviewed_ids", []),
        "knowledge_status": seeded.get("knowledge_status"),
        "unmapped": unmapped,
        "taxonomy_version": taxonomy.version,
        "source": source,
    }


@router.post("/projects/{project_id}/knowledge/retrieve")
async def retrieve_knowledge(
    project_id: str,
    body: RetrieveBody,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Which confirmed cases would ground an analysis of this text (debugging RAG)."""
    await core._require_project_role(project_id, current, database, _ANALYSTS)
    _, result = await core._call_ai_knowledge(
        "POST", f"/internal/projects/{project_id}/knowledge/retrieve", json_body=body.model_dump()
    )
    taxonomy = await get_current_taxonomy(database, project_id)

    def coded(kind: TaxonomyKind, code: Any) -> dict[str, Any] | None:
        return {"code": code, "label": taxonomy.label(kind, code) or code} if code else None

    cases = [
        {
            "id": case.get("knowledge_entry_id"),
            "defect_id": case.get("defect_id"),
            "title": case.get("title"),
            "module": case.get("module"),
            "score": case.get("similarity"),
            "final": {
                "root_cause": coded("root_causes", case.get("root_cause_code")),
                "sdlc_phase": coded("sdlc_phases", case.get("sdlc_phase_code")),
                "testing_gap": coded("testing_gaps", case.get("testing_gap_code")),
            },
            "summary": case.get("summary"),
            "source": case.get("source"),
        }
        for case in result.get("cases", [])
    ]
    return {"cases": cases, "top_k": result.get("top_k"), "threshold": result.get("threshold")}


@router.get("/projects/{project_id}/knowledge/stats")
async def knowledge_stats(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _READERS)
    _, result = await core._call_ai_knowledge(
        "GET", f"/internal/projects/{project_id}/knowledge/stats"
    )
    return result


@router.post("/projects/{project_id}/knowledge/reembed", status_code=202)
async def reembed_knowledge(
    project_id: str,
    body: ReembedBody,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Embed pending entries, or all entries after an embedding-model change (job)."""
    await core._require_project_role(project_id, current, database, _ANALYSTS)
    _, result = await core._call_ai_knowledge(
        "POST", f"/internal/projects/{project_id}/knowledge/reembed", json_body=body.model_dump()
    )
    await write_audit(
        database,
        action="KNOWLEDGE_REEMBED_REQUESTED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="jobs",
        entity_id=result.get("job_id"),
        detail={"force": body.force},
    )
    return result
