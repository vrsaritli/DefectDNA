"""Internal (service-token only) API of the trends-service."""

import re
from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from libs.common.config import settings
from libs.common.service_auth import verify_service_token
from libs.common.trends import DIMENSIONS, TrendFact
from services.trends_service.app import repository

router = APIRouter()
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
_MONTH_PATTERN = r"^\d{4}-\d{2}$"


class ReplaceFactsRequest(BaseModel):
    facts: list[TrendFact] = Field(max_length=100_000)


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


def _filters(
    module: str | None,
    release: str | None,
    month_from: str | None,
    month_to: str | None,
    root_cause: str | None,
    testing_gap: str | None,
    severity: str | None,
) -> dict[str, str | None]:
    return {
        "module": module,
        "release": release,
        "month_from": month_from,
        "month_to": month_to,
        "root_cause": root_cause,
        "testing_gap": testing_gap,
        "severity": severity,
    }


MonthQuery = Annotated[str | None, Query(pattern=_MONTH_PATTERN)]


@router.get("/internal/projects/{project_id}/trends")
async def trends(
    request: Request,
    project_id: str,
    group_by: str = "module,month",
    module: str | None = None,
    release: str | None = None,
    month_from: MonthQuery = None,
    month_to: MonthQuery = None,
    root_cause: str | None = None,
    testing_gap: str | None = None,
    severity: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    dimensions = [part.strip() for part in group_by.split(",") if part.strip()]
    if not 1 <= len(dimensions) <= 3 or any(d not in DIMENSIONS for d in dimensions):
        raise HTTPException(status_code=422, detail={"code": "INVALID_GROUP_BY"})
    if len(set(dimensions)) != len(dimensions):
        raise HTTPException(status_code=422, detail={"code": "INVALID_GROUP_BY"})
    rows = await repository.aggregate(
        _database(request),
        project_id,
        dimensions,
        _filters(module, release, month_from, month_to, root_cause, testing_gap, severity),
    )
    return {"group_by": dimensions, "rows": rows}


@router.get("/internal/projects/{project_id}/trends/summary")
async def trends_summary(
    request: Request,
    project_id: str,
    module: str | None = None,
    release: str | None = None,
    month_from: MonthQuery = None,
    month_to: MonthQuery = None,
    root_cause: str | None = None,
    testing_gap: str | None = None,
    severity: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    return await repository.summary(
        _database(request),
        project_id,
        _filters(module, release, month_from, month_to, root_cause, testing_gap, severity),
    )


@router.get("/internal/projects/{project_id}/trend-facts")
async def trend_facts(
    request: Request,
    project_id: str,
    module: str | None = None,
    release: str | None = None,
    month_from: MonthQuery = None,
    month_to: MonthQuery = None,
    limit: Annotated[int, Query(ge=1, le=100_000)] = 10_000,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    facts = await repository.list_facts(
        _database(request),
        project_id,
        _filters(module, release, month_from, month_to, None, None, None),
        limit,
    )
    return {"facts": facts}


@router.put("/internal/projects/{project_id}/trend-facts")
async def replace_trend_facts(
    request: Request,
    project_id: str,
    body: ReplaceFactsRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_gateway(authorization)
    _validate_project_id(project_id)
    if any(fact.project_id != project_id for fact in body.facts):
        raise HTTPException(status_code=422, detail={"code": "PROJECT_MISMATCH"})
    count = await repository.replace_project_facts(_database(request), project_id, body.facts)
    return {"project_id": project_id, "facts": count}
