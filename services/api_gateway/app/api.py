import asyncio
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Annotated, Any

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field, SecretStr, field_validator
from pymongo.errors import DuplicateKeyError

from libs.common.config import settings
from libs.common.service_auth import create_service_token
from services.api_gateway.app.auth.audit import write_audit
from services.api_gateway.app.auth.dependencies import (
    AdminDependency,
    CurrentUserDependency,
    DatabaseDependency,
    PasswordChangeUserDependency,
    ProjectAdminDependency,
)
from services.api_gateway.app.auth.passwords import (
    PasswordPolicyError,
    generate_temporary_password,
    hash_password,
    verify_password,
)
from services.api_gateway.app.auth.provider import AccountLocked, AuthProvider, LocalAuthProvider
from services.api_gateway.app.auth.tokens import (
    create_session_token,
    hash_csrf_token,
)
from services.api_gateway.app.taxonomy import enrich_rca

router = APIRouter(prefix="/api/v1")
_USERNAME_PATTERN = re.compile(r"^[a-z0-9._@+-]{3,254}$")
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


class Role(str, Enum):
    VIEWER = "VIEWER"
    QA_LEAD = "QA_LEAD"
    ADMIN = "ADMIN"


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("Username must be an email-style name or handle")
        return normalized


class ChangePasswordRequest(BaseModel):
    current_password: SecretStr = Field(max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("Username must be an email-style name or handle")
        return normalized

    @field_validator("display_name")
    @classmethod
    def trim_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Display name is required")
        return value


class SetRoleRequest(BaseModel):
    role: Role


class ResetPasswordRequest(BaseModel):
    project_id: str = Field(min_length=2, max_length=63)


class CreateProjectRequest(BaseModel):
    project_id: str = Field(min_length=2, max_length=63)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        value = value.strip().casefold()
        if not _PROJECT_ID_PATTERN.fullmatch(value):
            raise ValueError("Project ID must be a lowercase slug")
        return value

    @field_validator("name")
    @classmethod
    def trim_project_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Project name is required")
        return value


def _session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "defectdna_session",
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        "defectdna_session",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def _user_summary(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(user["_id"]),
        "username": user["username"],
        "display_name": user["display_name"],
        "must_change_password": user.get("must_change_password", False),
    }


def _object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except InvalidId as exc:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"}) from exc


async def _require_project_role(
    project_id: str,
    current: dict[str, Any],
    database: Any,
    allowed_roles: set[str],
) -> None:
    role = await database["user_project_roles"].find_one(
        {"user_id": current["user"]["_id"], "project_id": project_id}
    )
    if role is None or role["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN"})


async def _call_internal_service(
    base_url: str,
    service: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    data: dict[str, str] | None = None,
    json_body: Any = None,
) -> tuple[int, dict[str, Any]]:
    if len(settings.service_token_secret.encode("utf-8")) < 32:
        raise HTTPException(status_code=503, detail={"code": "SERVICE_AUTH_NOT_CONFIGURED"})
    service_headers = {
        **(headers or {}),
        "Authorization": f"Bearer {create_service_token(settings.service_token_secret, 'api-gateway')}",
    }
    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0),
        ) as client:
            response = await client.request(
                method,
                path,
                headers=service_headers,
                params=params,
                files=files,
                data=data,
                json=json_body,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail={"code": f"{service}_UNAVAILABLE"}) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail={"code": f"INVALID_{service}_RESPONSE"}) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail={"code": f"INVALID_{service}_RESPONSE"})
    if response.is_error:
        detail = payload.get("detail", {})
        detail = detail if isinstance(detail, dict) else {}
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "code": detail.get("code", f"{service}_REQUEST_FAILED"),
                **({"message": detail["message"]} if "message" in detail else {}),
            },
        )
    return response.status_code, payload


async def _call_ingestion(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    data: dict[str, str] | None = None,
    json_body: Any = None,
) -> tuple[int, dict[str, Any]]:
    return await _call_internal_service(
        settings.ingestion_service_url,
        "INGESTION",
        method,
        path,
        headers=headers,
        params=params,
        files=files,
        data=data,
        json_body=json_body,
    )


async def _call_ai_knowledge(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> tuple[int, dict[str, Any]]:
    return await _call_internal_service(
        settings.ai_knowledge_service_url,
        "AI_KNOWLEDGE",
        method,
        path,
        params=params,
        json_body=json_body,
    )


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    response: Response,
    database: DatabaseDependency,
) -> dict[str, Any]:
    if len(settings.session_signing_key.encode("utf-8")) < 32:
        raise HTTPException(status_code=503, detail={"code": "AUTH_NOT_CONFIGURED"})
    provider: AuthProvider = LocalAuthProvider(
        database, settings.login_max_failures, settings.login_lockout_minutes
    )
    try:
        user = await provider.authenticate(body.username, body.password.get_secret_value())
    except AccountLocked as exc:
        await write_audit(
            database,
            action="LOGIN_LOCKED",
            detail={"username": body.username},
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "ACCOUNT_LOCKED",
                "retry_after_seconds": exc.retry_after_seconds,
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    if user is None:
        await write_audit(
            database,
            action="LOGIN_FAILED",
            detail={"username": body.username},
        )
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})

    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.session_ttl_seconds)
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    await database["sessions"].insert_one(
        {
            "_id": session_id,
            "user_id": user["_id"],
            "csrf_token_hash": hash_csrf_token(csrf_token),
            "created_at": now,
            "last_seen_at": now,
            "expires_at": expires_at,
            "revoked_at": None,
        }
    )
    token = create_session_token(
        session_id, str(user["_id"]), settings.session_signing_key, settings.session_ttl_seconds
    )
    _session_cookie(response, token)
    await write_audit(
        database,
        action="LOGIN_SUCCEEDED",
        actor_user_id=user["_id"],
        entity="users",
        entity_id=user["_id"],
    )
    return {"user": _user_summary(user), "csrf_token": csrf_token}


@router.post("/auth/logout", status_code=204)
async def logout(
    response: Response,
    current: PasswordChangeUserDependency,
    database: DatabaseDependency,
) -> Response:
    now = datetime.now(UTC)
    await database["sessions"].update_one(
        {"_id": current["session"]["_id"]}, {"$set": {"revoked_at": now}}
    )
    await write_audit(
        database,
        action="LOGOUT",
        actor_user_id=current["user"]["_id"],
        entity="sessions",
        entity_id=current["session"]["_id"],
    )
    _clear_session_cookie(response)
    response.status_code = 204
    return response


@router.post("/auth/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current: PasswordChangeUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    old_password = body.current_password.get_secret_value()
    if not await asyncio.to_thread(verify_password, old_password, current["user"]["password_hash"]):
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    new_password = body.new_password.get_secret_value()
    try:
        new_hash = hash_password(new_password, current["user"]["username"])
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "PASSWORD_POLICY", "message": str(exc)}
        ) from exc

    now = datetime.now(UTC)
    await database["users"].update_one(
        {"_id": current["user"]["_id"]},
        {
            "$set": {
                "password_hash": new_hash,
                "must_change_password": False,
                "failed_login_count": 0,
                "locked_until": None,
                "updated_at": now,
            }
        },
    )
    await database["sessions"].update_many(
        {"user_id": current["user"]["_id"], "_id": {"$ne": current["session"]["_id"]}},
        {"$set": {"revoked_at": now}},
    )
    csrf_token = secrets.token_urlsafe(32)
    await database["sessions"].update_one(
        {"_id": current["session"]["_id"]},
        {"$set": {"csrf_token_hash": hash_csrf_token(csrf_token), "last_seen_at": now}},
    )
    await write_audit(
        database,
        action="PASSWORD_CHANGED",
        actor_user_id=current["user"]["_id"],
        entity="users",
        entity_id=current["user"]["_id"],
    )
    return {"status": "ok", "csrf_token": csrf_token}


@router.get("/me")
async def me(
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    roles = await database["user_project_roles"].find(
        {"user_id": current["user"]["_id"]}
    ).to_list(length=None)
    return {
        "user": _user_summary(current["user"]),
        "project_roles": [
            {"project_id": role["project_id"], "role": role["role"]} for role in roles
        ],
    }


@router.get("/projects")
async def list_projects(
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    roles = await database["user_project_roles"].find(
        {"user_id": current["user"]["_id"]}
    ).to_list(length=None)
    project_ids = [role["project_id"] for role in roles]
    projects = await database["projects"].find({"_id": {"$in": project_ids}}).to_list(
        length=None
    )
    project_by_id = {project["_id"]: project for project in projects}
    return {
        "projects": [
            {
                "id": role["project_id"],
                "name": project_by_id.get(role["project_id"], {}).get(
                    "name", role["project_id"]
                ),
                "role": role["role"],
            }
            for role in roles
        ]
    }


@router.post("/projects", status_code=201)
async def create_project(
    body: CreateProjectRequest,
    current: AdminDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    project = {
        "_id": body.project_id,
        "name": body.name,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await database["projects"].insert_one(project)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail={"code": "PROJECT_EXISTS"}) from exc
    await database["user_project_roles"].insert_one(
        {
            "user_id": current["user"]["_id"],
            "project_id": body.project_id,
            "role": Role.ADMIN.value,
            "created_at": now,
            "updated_at": now,
        }
    )
    await write_audit(
        database,
        action="PROJECT_CREATED",
        actor_user_id=current["user"]["_id"],
        project_id=body.project_id,
        entity="projects",
        entity_id=body.project_id,
    )
    return {"id": body.project_id, "name": body.name, "role": Role.ADMIN.value}


@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    current: AdminDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    temporary_password = generate_temporary_password(body.username)
    now = datetime.now(UTC)
    user = {
        "username": body.username,
        "display_name": body.display_name,
        "password_hash": hash_password(temporary_password, body.username),
        "must_change_password": True,
        "is_active": True,
        "failed_login_count": 0,
        "locked_until": None,
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await database["users"].insert_one(user)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail={"code": "USERNAME_EXISTS"}) from exc
    user["_id"] = result.inserted_id
    await write_audit(
        database,
        action="USER_CREATED",
        actor_user_id=current["user"]["_id"],
        entity="users",
        entity_id=result.inserted_id,
        detail={"username": body.username},
    )
    return {"user": _user_summary(user), "temporary_password": temporary_password}


@router.put("/users/{user_id}/roles/{project_id}")
async def set_project_role(
    user_id: str,
    project_id: str,
    body: SetRoleRequest,
    current: ProjectAdminDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    target_id = _object_id(user_id)
    if await database["users"].find_one({"_id": target_id}) is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    if await database["projects"].find_one({"_id": project_id}) is None:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    now = datetime.now(UTC)
    await database["user_project_roles"].update_one(
        {"user_id": target_id, "project_id": project_id},
        {
            "$set": {"role": body.role.value, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    await write_audit(
        database,
        action="ROLE_CHANGED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="user_project_roles",
        entity_id=target_id,
        detail={"role": body.role.value},
    )
    return {"user_id": user_id, "project_id": project_id, "role": body.role.value}


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    body: ResetPasswordRequest,
    current: AdminDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    target_id = _object_id(user_id)
    project_id = body.project_id.strip().casefold()
    if not _PROJECT_ID_PATTERN.fullmatch(project_id):
        raise HTTPException(status_code=422, detail={"code": "INVALID_PROJECT_ID"})
    admin_roles = await database["user_project_roles"].find(
        {"user_id": current["user"]["_id"], "role": "ADMIN"}
    ).to_list(length=None)
    admin_projects = [role["project_id"] for role in admin_roles]
    if project_id not in admin_projects:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN"})
    target_role = await database["user_project_roles"].find_one(
        {"user_id": target_id, "project_id": project_id}
    )
    user = await database["users"].find_one({"_id": target_id})
    if user is None or target_role is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})

    temporary_password = generate_temporary_password(user["username"])
    now = datetime.now(UTC)
    await database["users"].update_one(
        {"_id": target_id},
        {
            "$set": {
                "password_hash": hash_password(temporary_password, user["username"]),
                "must_change_password": True,
                "failed_login_count": 0,
                "locked_until": None,
                "updated_at": now,
            }
        },
    )
    await database["sessions"].update_many(
        {"user_id": target_id, "revoked_at": None},
        {"$set": {"revoked_at": now}},
    )
    await write_audit(
        database,
        action="PASSWORD_RESET",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="users",
        entity_id=target_id,
    )
    return {"user_id": user_id, "temporary_password": temporary_password}


@router.post("/projects/{project_id}/imports", status_code=202)
async def upload_defects(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    file: UploadFile = File(...),
    mapping: str | None = Form(default=None),
) -> dict[str, Any]:
    await _require_project_role(project_id, current, database, {"QA_LEAD", "ADMIN"})
    content = await file.read(settings.max_import_file_bytes + 1)
    if len(content) > settings.max_import_file_bytes:
        raise HTTPException(status_code=413, detail={"code": "FILE_TOO_LARGE"})
    _, result = await _call_ingestion(
        "POST",
        "/internal/imports",
        headers={
            "X-Project-ID": project_id,
            "X-User-ID": str(current["user"]["_id"]),
        },
        files={
            "file": (
                file.filename or "",
                content,
                file.content_type or "application/octet-stream",
            )
        },
        data={"mapping": mapping} if mapping is not None else None,
    )
    preview = result.get("preview", {})
    await write_audit(
        database,
        action="DEFECTS_IMPORTED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="import_batches",
        entity_id=result.get("import_batch_id"),
        detail={
            "source": "CSV_IMPORT",
            "rows_total": preview.get("rows_total", 0),
            "rows_valid": preview.get("rows_valid", 0),
            "row_errors": len(preview.get("row_errors", [])),
        },
    )
    return result


@router.post("/projects/{project_id}/ado-sync", status_code=202)
async def sync_ado_defects(
    project_id: str,
    start_date: date,
    end_date: date,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await _require_project_role(project_id, current, database, {"QA_LEAD", "ADMIN"})
    _, result = await _call_ingestion(
        "POST",
        "/internal/ado-sync",
        headers={
            "X-Project-ID": project_id,
            "X-User-ID": str(current["user"]["_id"]),
        },
        params={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
    )
    await write_audit(
        database,
        action="ADO_DEFECTS_SYNCED",
        actor_user_id=current["user"]["_id"],
        project_id=project_id,
        entity="import_batches",
        entity_id=result.get("import_batch_id"),
        detail={"rows_total": result.get("rows_total", 0)},
    )
    return result


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    job_object_id = _object_id(job_id)
    job = await database["jobs"].find_one({"_id": job_object_id})
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND"})
    project_id = job.get("payload", {}).get("project_id")
    if not isinstance(project_id, str):
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND"})
    await _require_project_role(
        project_id, current, database, {"VIEWER", "QA_LEAD", "ADMIN"}
    )
    return {
        "job_id": str(job["_id"]),
        "type": job["type"],
        "status": job["status"],
        "progress": job.get("payload", {}).get("progress", {}),
        "errors": job.get("payload", {}).get("errors", []),
    }


@router.get("/projects/{project_id}/defects")
async def list_project_defects(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
    module: str | None = None,
    release: str | None = None,
    state: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    await _require_project_role(
        project_id, current, database, {"VIEWER", "QA_LEAD", "ADMIN"}
    )
    _, result = await _call_ingestion(
        "GET",
        f"/internal/projects/{project_id}/defects",
        headers={},
        params={
            "page": page,
            "page_size": page_size,
            **({"module": module} if module else {}),
            **({"release": release} if release else {}),
            **({"state": state} if state else {}),
        },
    )
    return result


@router.get("/projects/{project_id}/defects/{ado_id}")
async def get_project_defect(
    project_id: str,
    ado_id: int,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    await _require_project_role(
        project_id, current, database, {"VIEWER", "QA_LEAD", "ADMIN"}
    )
    _, result = await _call_ingestion(
        "GET",
        f"/internal/projects/{project_id}/defects/{ado_id}",
        headers={},
    )
    try:
        _, rca_result = await _call_ai_knowledge(
            "GET", f"/internal/projects/{project_id}/defects/{ado_id}/rca"
        )
    except HTTPException as exc:
        if exc.status_code not in (502, 503):
            raise
        result["rca"] = None
        result["rca_unavailable"] = True
        return result
    rca = rca_result.get("rca")
    result["rca"] = (
        await enrich_rca(database, project_id, rca) if isinstance(rca, dict) else None
    )
    return result
