"""Azure DevOps connection and pull routes for the Import screen.

The PAT never passes through the browser or this gateway: ingestion always uses the
server's ``ADO_PAT``. Projects may override the organization/project (Admin); otherwise
the server defaults from ``.env`` apply.
"""

from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from libs.common.config import settings
from services.api_gateway.app import api as core
from services.api_gateway.app.auth.audit import write_audit
from services.api_gateway.app.auth.dependencies import (
    CurrentUserDependency,
    DatabaseDependency,
)
from services.api_gateway.app.auth.rate_limit import enforce_quota

router = APIRouter(prefix="/api/v1")
_ANALYSTS = {"QA_LEAD", "ADMIN"}


class AdoConnectionBody(BaseModel):
    organization: str = Field(min_length=1, max_length=300)
    project: str = Field(min_length=1, max_length=200)

    @field_validator("organization", "project")
    @classmethod
    def strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Required")
        return value


class AdoPullBody(BaseModel):
    mode: Literal["query", "ids", "closed"]
    query_link: str | None = Field(default=None, max_length=2_000)
    work_item_ids: str | None = Field(default=None, max_length=50_000)
    start_date: date | None = None
    end_date: date | None = None
    include_comments: bool = True


async def _target(database: Any, project_id: str) -> dict[str, Any]:
    project = await database["projects"].find_one({"_id": project_id}, {"ado": 1})
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    saved = project.get("ado") or {}
    return {
        "organization": saved.get("organization") or None,
        "project": saved.get("project") or None,
        "source": "project" if saved.get("organization") else "server default",
    }


@router.get("/projects/{project_id}/ado-connection")
async def get_ado_connection(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Live connection status using the server PAT (checked on every call)."""
    await core._require_project_role(project_id, current, database, _ANALYSTS)
    target = await _target(database, project_id)
    _, status = await core._call_ingestion(
        "POST",
        "/internal/ado/status",
        json_body={"organization": target["organization"], "project": target["project"]},
    )
    return {**status, "source": target["source"], "checked_at": datetime.now(UTC)}


@router.put("/projects/{project_id}/ado-connection")
async def set_ado_connection(
    project_id: str,
    body: AdoConnectionBody,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Point this project at an ADO organization/project (Admin). Verified before saving."""
    await core._require_project_role(project_id, current, database, {"ADMIN"})
    _, status = await core._call_ingestion(
        "POST", "/internal/ado/status", json_body=body.model_dump()
    )
    if not status.get("connected"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": status.get("error") or "ADO_NOT_CONNECTED",
                "message": status.get("message") or "Could not connect with the server PAT",
            },
        )
    await database["projects"].update_one(
        {"_id": project_id},
        {
            "$set": {
                "ado": {"organization": body.organization, "project": body.project},
                "updated_at": datetime.now(UTC),
            }
        },
    )
    await write_audit(
        database,
        action="ADO_CONNECTION_CHANGED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="projects",
        entity_id=project_id,
        detail={"organization": body.organization, "project": body.project},
    )
    return {**status, "source": "project", "checked_at": datetime.now(UTC)}


@router.post("/projects/{project_id}/ado-pull", status_code=202)
async def pull_from_ado(
    project_id: str,
    body: AdoPullBody,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Import defects from a saved query link, work item IDs, or closed-by-date."""
    await core._require_project_role(project_id, current, database, _ANALYSTS)
    await enforce_quota(
        database,
        scope=f"upload:{current['user']['_id']}",
        limit=settings.upload_quota_per_hour,
        window_seconds=3600,
    )
    target = await _target(database, project_id)
    _, result = await core._call_ingestion(
        "POST",
        "/internal/ado-pull",
        headers={"X-Project-ID": project_id, "X-User-ID": str(current["user"]["_id"])},
        json_body={
            **body.model_dump(mode="json"),
            "organization": target["organization"],
            "project": target["project"],
        },
    )
    await write_audit(
        database,
        action="ADO_DEFECTS_PULLED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="import_batches",
        entity_id=result.get("import_batch_id"),
        detail={
            "mode": body.mode,
            "rows_total": result.get("rows_total", 0),
            "include_comments": body.include_comments,
        },
    )
    result["already_confirmed_ids"] = await core._confirmed_among(
        project_id, [int(item) for item in result.get("ado_ids", [])]
    )
    return result
