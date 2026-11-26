"""Phase 6 gateway routes: RCA review commit, trends, trend rebuild and export."""

import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from openpyxl import Workbook
from pydantic import AliasChoices, BaseModel, Field

from libs.common.config import settings
from libs.common.taxonomy import Taxonomy, TaxonomyKind
from libs.common.trends import DIMENSIONS
from services.api_gateway.app import api as core
from services.api_gateway.app.auth.audit import write_audit
from services.api_gateway.app.auth.dependencies import (
    CurrentUserDependency,
    DatabaseDependency,
)
from services.api_gateway.app.rca_api import _with_ado_root_causes, ado_root_causes
from services.api_gateway.app.taxonomy import (
    enrich_rca,
    get_current_taxonomy,
    get_taxonomy_version,
)

router = APIRouter(prefix="/api/v1")

_READERS = {"VIEWER", "QA_LEAD", "ADMIN"}
# §8: approve / correct / reject / re-open — QA Lead, and Admin (end-to-end access).
_REVIEWERS = {"QA_LEAD", "ADMIN"}
_CODE_FIELDS: dict[str, TaxonomyKind] = {
    "root_cause_code": "root_causes",
    "sdlc_phase_code": "sdlc_phases",
    "testing_gap_code": "testing_gaps",
}
_DIMENSION_KIND: dict[str, TaxonomyKind] = {
    "root_cause": "root_causes",
    "sdlc_phase": "sdlc_phases",
    "testing_gap": "testing_gaps",
}
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_MONTH = r"^\d{4}-\d{2}$"
TRENDS_NOTE = "Counts include approved and corrected RCAs only."


class FinalCodes(BaseModel):
    root_cause_code: str = Field(min_length=1, max_length=64)
    sdlc_phase_code: str = Field(min_length=1, max_length=64)
    # Optional: the review screen no longer asks for a testing gap.
    testing_gap_code: str | None = Field(default=None, min_length=1, max_length=64)
    # Accepted for compatibility with POSTMAN_PHASE5.md "correction" bodies.
    rationale: str | None = Field(default=None, max_length=2_000, exclude=True)


class ReviewBody(BaseModel):
    decision: Literal["APPROVE", "CORRECT", "REJECT"] = Field(
        validation_alias=AliasChoices("decision", "action")
    )
    final: FinalCodes | None = Field(
        default=None, validation_alias=AliasChoices("final", "correction")
    )
    note: str | None = Field(default=None, max_length=2_000)
    reopen: bool = False


# ---------------------------------------------------------------- review commit


async def _load_rca(rca_id: str, expected_project: str | None = None) -> dict[str, Any]:
    _, found = await core._call_ai_knowledge("GET", f"/internal/rcas/{rca_id}")
    rca = found.get("rca")
    if not isinstance(rca, dict) or not isinstance(rca.get("project_id"), str):
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"})
    if expected_project is not None and rca["project_id"] != expected_project:
        raise HTTPException(status_code=404, detail={"code": "RCA_NOT_FOUND"})
    return rca


async def _defect_text(project_id: str, ado_id: Any) -> dict[str, Any] | None:
    """Defect text for the knowledge entry; review still works if ingestion is down."""
    try:
        _, result = await core._call_ingestion(
            "GET", f"/internal/projects/{project_id}/defects/{ado_id}"
        )
    except HTTPException:
        return None
    defect = result.get("defect") or {}
    return {
        key: defect.get(key) or ""
        for key in ("title", "description", "repro_steps", "comments", "module", "release")
    }


@router.post("/projects/{project_id}/rcas/{rca_id}/review")
async def review_project_rca(
    project_id: str,
    rca_id: str,
    body: ReviewBody,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Project-scoped alias of POST /rcas/{rca_id}/review (POSTMAN_PHASE5.md path)."""
    await _load_rca(rca_id, project_id)
    return await review_rca(rca_id, body, current, database)


@router.post("/rcas/{rca_id}/review")
async def review_rca(
    rca_id: str,
    body: ReviewBody,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    rca = await _load_rca(rca_id)
    project_id: str = rca["project_id"]
    await core._require_project_role(project_id, current, database, _REVIEWERS)
    if body.note is None and body.final is not None and body.final.rationale:
        body.note = body.final.rationale

    version = rca.get("taxonomy_version")
    taxonomy = (
        await get_taxonomy_version(database, project_id, version)
        if isinstance(version, int)
        else None
    )
    if taxonomy is None:
        raise HTTPException(status_code=409, detail={"code": "TAXONOMY_VERSION_MISSING"})
    if body.decision == "CORRECT":
        if body.final is None:
            raise HTTPException(status_code=422, detail={"code": "FINAL_REQUIRED"})
        for name, kind in _CODE_FIELDS.items():
            value = getattr(body.final, name)
            if value is None and name == "testing_gap_code":
                continue
            if value not in taxonomy.codes(kind):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "UNKNOWN_TAXONOMY_CODE", "message": f"Unknown {name}"},
                )

    _, result = await core._call_ai_knowledge(
        "POST",
        f"/internal/rcas/{rca_id}/review",
        json_body={
            "decision": body.decision,
            "final": body.final.model_dump() if body.final else None,
            "note": body.note,
            "reviewer_id": str(current["user"]["_id"]),
            "reopen": body.reopen,
            "taxonomy": taxonomy.to_payload(),
            "defect": await _defect_text(project_id, rca.get("defect_id")),
        },
    )
    audit_action = {"APPROVED": "RCA_APPROVED", "REJECTED": "RCA_REJECTED"}.get(
        str(result.get("status")), "RCA_CORRECTED"
    )
    await write_audit(
        database,
        action=audit_action,
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="rca_analyses",
        entity_id=rca_id,
        detail={
            "defect_id": result.get("defect_id"),
            "previous_status": result.get("previous_status"),
            "reopen": body.reopen,
        },
    )
    _, refreshed = await core._call_ai_knowledge("GET", f"/internal/rcas/{rca_id}")
    view = await enrich_rca(database, project_id, refreshed["rca"])
    await _with_ado_root_causes(project_id, [view])
    view["trend_job_id"] = result.get("trend_job_id")
    view["knowledge_entry_id"] = result.get("knowledge_entry_id")
    view["knowledge_status"] = result.get("knowledge_status")
    return view


async def _reopen(
    rca_id: str, current: dict[str, Any], database: Any, expected_project: str | None
) -> dict[str, Any]:
    rca = await _load_rca(rca_id, expected_project)
    project_id: str = rca["project_id"]
    await core._require_project_role(project_id, current, database, _REVIEWERS)
    _, result = await core._call_ai_knowledge(
        "POST",
        f"/internal/rcas/{rca_id}/reopen",
        json_body={"reviewer_id": str(current["user"]["_id"])},
    )
    await write_audit(
        database,
        action="RCA_REOPENED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="rca_analyses",
        entity_id=rca_id,
        detail={"defect_id": result.get("defect_id"), "previous_status": result.get("previous_status")},
    )
    _, refreshed = await core._call_ai_knowledge("GET", f"/internal/rcas/{rca_id}")
    view = await enrich_rca(database, project_id, refreshed["rca"])
    await _with_ado_root_causes(project_id, [view])
    return view


@router.post("/rcas/{rca_id}/reopen")
async def reopen_rca(
    rca_id: str, current: CurrentUserDependency, database: DatabaseDependency
) -> dict[str, Any]:
    """Undo a review: back to the AI proposal, out of the knowledge base and trends."""
    return await _reopen(rca_id, current, database, None)


@router.post("/projects/{project_id}/rcas/{rca_id}/reopen")
async def reopen_project_rca(
    project_id: str, rca_id: str, current: CurrentUserDependency, database: DatabaseDependency
) -> dict[str, Any]:
    return await _reopen(rca_id, current, database, project_id)


# ---------------------------------------------------------------------- trends


def _filter_params(
    module: str | None,
    release: str | None,
    month_from: str | None,
    month_to: str | None,
    root_cause: str | None,
    testing_gap: str | None,
    severity: str | None,
) -> dict[str, str]:
    values = {
        "module": module,
        "release": release,
        "month_from": month_from,
        "month_to": month_to,
        "root_cause": root_cause,
        "testing_gap": testing_gap,
        "severity": severity,
    }
    return {key: value for key, value in values.items() if value}


def _labels(taxonomy: Taxonomy) -> dict[str, dict[str, str]]:
    return {
        name: {item.code: item.label for item in taxonomy.items(kind)}
        for name, kind in _DIMENSION_KIND.items()
    }


MonthQuery = Annotated[str | None, Query(pattern=_MONTH)]


@router.get("/projects/{project_id}/trends")
async def get_trends(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    group_by: str = "module,month",
    module: str | None = None,
    release: str | None = None,
    month_from: MonthQuery = None,
    month_to: MonthQuery = None,
    root_cause: str | None = None,
    testing_gap: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _READERS)
    dimensions = [part.strip() for part in group_by.split(",") if part.strip()]
    if not 1 <= len(dimensions) <= 3 or any(d not in DIMENSIONS for d in dimensions):
        raise HTTPException(status_code=422, detail={"code": "INVALID_GROUP_BY"})
    _, result = await core._call_trends(
        "GET",
        f"/internal/projects/{project_id}/trends",
        params={
            "group_by": ",".join(dimensions),
            **_filter_params(
                module, release, month_from, month_to, root_cause, testing_gap, severity
            ),
        },
    )
    taxonomy = await get_current_taxonomy(database, project_id)
    return {**result, "labels": _labels(taxonomy), "note": TRENDS_NOTE}


@router.get("/projects/{project_id}/trends/summary")
async def get_trends_summary(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    module: str | None = None,
    release: str | None = None,
    month_from: MonthQuery = None,
    month_to: MonthQuery = None,
    root_cause: str | None = None,
    testing_gap: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    await core._require_project_role(project_id, current, database, _READERS)
    _, result = await core._call_trends(
        "GET",
        f"/internal/projects/{project_id}/trends/summary",
        params=_filter_params(
            module, release, month_from, month_to, root_cause, testing_gap, severity
        ),
    )
    taxonomy = await get_current_taxonomy(database, project_id)
    return {**result, "labels": _labels(taxonomy), "note": TRENDS_NOTE}


@router.post("/projects/{project_id}/trends/rebuild")
async def rebuild_trends(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Re-derive trend facts from reviewed RCAs (reconciliation / recovery)."""
    await core._require_project_role(project_id, current, database, {"QA_LEAD", "ADMIN"})
    _, source = await core._call_ai_knowledge(
        "GET", f"/internal/projects/{project_id}/reviewed-facts"
    )
    facts = source.get("facts", [])
    _, result = await core._call_trends(
        "PUT", f"/internal/projects/{project_id}/trend-facts", json_body={"facts": facts}
    )
    await write_audit(
        database,
        action="TRENDS_REBUILT",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="trend_facts",
        entity_id=project_id,
        detail={"facts": result.get("facts", len(facts))},
    )
    return {"project_id": project_id, "facts": result.get("facts", len(facts))}


# ---------------------------------------------------------------------- export


def safe_cell(value: Any) -> Any:
    """Neutralise spreadsheet formulas (CSV/Excel injection) and flatten values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        value = "; ".join(str(item) for item in value)
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


async def _paged(
    call: Any, path: str, key: str, limit: int, params: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < limit:
        _, result = await call("GET", path, params={**(params or {}), "page": page, "page_size": 100})
        batch = result.get(key, [])
        rows.extend(batch)
        if len(batch) < 100 or len(rows) >= int(result.get("total", 0)):
            return rows[:limit], len(rows) > limit
        page += 1
    return rows[:limit], True


def _label(taxonomy: Taxonomy, kind: TaxonomyKind, code: Any) -> str:
    return (taxonomy.label(kind, code) or str(code)) if code else ""


async def _export_rows(
    database: Any, project_id: str, export_type: str, group_by: str
) -> tuple[list[str], list[list[Any]], bool]:
    limit = settings.export_max_rows
    if export_type == "defects":
        defects, truncated = await _paged(
            core._call_ingestion, f"/internal/projects/{project_id}/defects", "defects", limit
        )
        header = [
            "ado_id", "title", "module", "release", "severity", "priority", "state",
            "created_date", "closed_date", "tags",
        ]
        return header, [[d.get(column) for column in header] for d in defects], truncated

    taxonomy = await get_current_taxonomy(database, project_id)
    if export_type == "rcas":
        rcas, truncated = await _paged(
            core._call_ai_knowledge, f"/internal/projects/{project_id}/rcas", "rcas", limit
        )
        header = [
            "rca_id", "defect_id", "title", "module", "release", "status",
            "ai_root_cause", "ado_root_cause", "ai_sdlc_phase", "ai_confidence",
            "ai_rationale", "ai_evidence", "final_root_cause", "final_sdlc_phase",
            "review_note", "failure_reason", "taxonomy_version", "model", "prompt_version",
            "analyzed_at", "reviewed_at",
        ]
        ado = await ado_root_causes(
            project_id, [r["defect_id"] for r in rcas if isinstance(r.get("defect_id"), int)]
        )
        rows = []
        for rca in rcas:
            proposed = rca.get("proposed") or {}
            final = rca.get("final") or {}
            snapshot = rca.get("defect_snapshot") or {}
            model = rca.get("model") or {}
            rows.append(
                [
                    rca.get("id"), rca.get("defect_id"), snapshot.get("title"),
                    snapshot.get("module"), snapshot.get("release"), rca.get("status"),
                    _label(taxonomy, "root_causes", proposed.get("root_cause_code")),
                    ado.get(str(rca.get("defect_id"))),
                    _label(taxonomy, "sdlc_phases", proposed.get("sdlc_phase_code")),
                    proposed.get("confidence"), proposed.get("rationale"),
                    ", ".join(proposed.get("evidence_fields") or []),
                    _label(taxonomy, "root_causes", final.get("root_cause_code")),
                    _label(taxonomy, "sdlc_phases", final.get("sdlc_phase_code")),
                    final.get("note"), rca.get("failure_reason"), rca.get("taxonomy_version"),
                    model.get("llm"), model.get("prompt_version"), rca.get("analyzed_at"),
                    rca.get("reviewed_at"),
                ]
            )
        return header, rows, truncated

    dimensions = [part.strip() for part in group_by.split(",") if part.strip()]
    if not 1 <= len(dimensions) <= 3 or any(d not in DIMENSIONS for d in dimensions):
        raise HTTPException(status_code=422, detail={"code": "INVALID_GROUP_BY"})
    _, result = await core._call_trends(
        "GET",
        f"/internal/projects/{project_id}/trends",
        params={"group_by": ",".join(dimensions)},
    )
    trend_rows = result.get("rows", [])
    codes = taxonomy.codes("root_causes")
    seen = {code for row in trend_rows for code in row.get("by_root_cause", {})}
    codes = [code for code in codes if code in seen] + sorted(seen - set(codes))

    def display(name: str, value: Any) -> Any:
        kind = _DIMENSION_KIND.get(name)
        return _label(taxonomy, kind, value) if kind else value

    header = [*dimensions, "total", *[_label(taxonomy, "root_causes", c) for c in codes]]
    rows = [
        [
            *[display(name, row.get(name)) for name in dimensions],
            row.get("total", 0),
            *[row.get("by_root_cause", {}).get(code, 0) for code in codes],
        ]
        for row in trend_rows[:limit]
    ]
    return header, rows, len(trend_rows) > limit


def _to_csv(header: list[str], rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([safe_cell(cell) for cell in header])
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")


def _to_xlsx(sheet_name: str, header: list[str], rows: list[list[Any]]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=sheet_name[:31])
    sheet.append([safe_cell(cell) for cell in header])
    for row in rows:
        sheet.append([safe_cell(cell) for cell in row])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@router.get("/projects/{project_id}/export")
async def export_data(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    type: Literal["defects", "rcas", "trends"] = "rcas",
    format: Literal["csv", "xlsx"] = "csv",
    group_by: str = "module,release,month",
) -> Response:
    await core._require_project_role(project_id, current, database, _READERS)
    header, rows, truncated = await _export_rows(database, project_id, type, group_by)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    filename = f"defectdna-{project_id}-{type}-{stamp}.{format}"
    if format == "csv":
        content = _to_csv(header, rows)
        media_type = "text/csv; charset=utf-8"
    else:
        content = _to_xlsx(type, header, rows)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    await write_audit(
        database,
        action="DATA_EXPORTED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="export",
        entity_id=type,
        detail={"type": type, "format": format, "rows": len(rows), "truncated": truncated},
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Rows": str(len(rows)),
            "X-Export-Truncated": "true" if truncated else "false",
        },
    )
