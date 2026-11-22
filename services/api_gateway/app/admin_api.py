"""Phase 7 gateway routes: delete project data (job) and the Operations view."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from libs.common.ops import auth_metrics, evaluate_alerts, overall_status
from services.api_gateway.app import api as core
from services.api_gateway.app.auth.audit import write_audit
from services.api_gateway.app.auth.dependencies import (
    CurrentUserDependency,
    DatabaseDependency,
)

router = APIRouter(prefix="/api/v1")
logger = structlog.get_logger()

DELETE_JOB = "delete_project_data"
# Analysis data first (queued jobs hold defect text), then derived trends, then sources.
_DELETE_STEPS: tuple[tuple[str, str], ...] = (
    ("ai-knowledge-service", "_call_ai_knowledge"),
    ("trends-service", "_call_trends"),
    ("ingestion-service", "_call_ingestion"),
)


class DeleteProjectDataBody(BaseModel):
    confirm_project_id: str = Field(min_length=2, max_length=63)


async def _run_delete(database: Any, job_id: ObjectId, project_id: str, actor: Any) -> None:
    deleted: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for index, (service, caller) in enumerate(_DELETE_STEPS, start=1):
        try:
            _, result = await getattr(core, caller)(
                "DELETE", f"/internal/projects/{project_id}/data"
            )
            deleted[service] = result.get("deleted", {})
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else "REQUEST_FAILED"
            errors.append({"item": service, "message": str(code)})
        await database["jobs"].update_one(
            {"_id": job_id},
            {
                "$set": {
                    "payload.progress": {
                        "total": len(_DELETE_STEPS),
                        "done": index,
                        "failed": len(errors),
                    },
                    "payload.errors": errors,
                    "payload.deleted": deleted,
                    "updated_at": datetime.now(UTC),
                }
            },
        )
    status = "FAILED" if errors else "SUCCEEDED"
    await database["jobs"].update_one(
        {"_id": job_id},
        {"$set": {"status": status, "updated_at": datetime.now(UTC), "lease_expires_at": None}},
    )
    await write_audit(
        database,
        action="PROJECT_DATA_DELETED" if not errors else "PROJECT_DATA_DELETION_FAILED",
        actor_user_id=actor,
        project_id=project_id,
        entity="jobs",
        entity_id=job_id,
        detail={"deleted": deleted, "errors": errors},
    )
    logger.info("project_data_deletion_finished", project_id=project_id, status=status)


@router.delete("/projects/{project_id}/data", status_code=202)
async def delete_project_data(
    project_id: str,
    body: DeleteProjectDataBody,
    background: BackgroundTasks,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """Delete a project's defects, RCAs, knowledge entries, trends and jobs (Admin).

    Users, roles, the project record, taxonomies and the append-only audit log are kept.
    Every step is idempotent, so a failed job can simply be requested again.
    """
    await core._require_project_role(project_id, current, database, {"ADMIN"})
    if body.confirm_project_id != project_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CONFIRMATION_MISMATCH",
                "message": "Type the project ID to confirm deletion",
            },
        )
    now = datetime.now(UTC)
    result = await database["jobs"].insert_one(
        {
            "type": DELETE_JOB,
            "payload": {
                "project_id": project_id,
                "requested_by": str(current["user"]["_id"]),
                "progress": {"total": len(_DELETE_STEPS), "done": 0, "failed": 0},
                "errors": [],
            },
            "status": "RUNNING",
            "attempts": 1,
            "owner": "api-gateway",
            "lease_expires_at": now + timedelta(minutes=10),
            "created_at": now,
            "updated_at": now,
        }
    )
    await write_audit(
        database,
        action="PROJECT_DATA_DELETION_REQUESTED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="jobs",
        entity_id=result.inserted_id,
    )
    background.add_task(
        _run_delete, database, result.inserted_id, project_id, current["user"]["_id"]
    )
    return {"job_id": str(result.inserted_id), "status": "RUNNING"}


@router.get("/projects/{project_id}/ops")
async def operations(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> dict[str, Any]:
    """Health of a project's pipeline plus alerts (Admin)."""
    await core._require_project_role(project_id, current, database, {"ADMIN"})
    _, metrics = await core._call_ai_knowledge(
        "GET", f"/internal/projects/{project_id}/ops/metrics", params={"hours": hours}
    )
    since = datetime.now(UTC) - timedelta(hours=hours)
    metrics["auth"] = await auth_metrics(database, since)
    alerts = evaluate_alerts(metrics)
    return {
        "status": overall_status(alerts),
        "alerts": [alert.to_dict() for alert in alerts],
        "metrics": metrics,
    }
