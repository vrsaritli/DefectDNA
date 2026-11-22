import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from libs.common.config import settings
from services.api_gateway.app.auth.tokens import hash_csrf_token, verify_session_token

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def get_database(request: Request) -> AsyncIOMotorDatabase[dict[str, Any]]:
    client = getattr(request.app.state, "mongo_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail={"code": "DATABASE_UNAVAILABLE"})
    return cast(AsyncIOMotorDatabase[dict[str, Any]], client[settings.mongodb_db])


DatabaseDependency = Annotated[
    AsyncIOMotorDatabase[dict[str, Any]], Depends(get_database)
]


async def _load_session_user(
    request: Request,
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    allow_password_change: bool,
) -> dict[str, Any]:
    token = request.cookies.get("defectdna_session")
    payload = verify_session_token(token or "", settings.session_signing_key)
    if payload is None:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED"})
    try:
        user_id = ObjectId(payload["sub"])
    except (InvalidId, TypeError) as exc:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED"}) from exc

    now = datetime.now(UTC)
    session = await database["sessions"].find_one(
        {
            "_id": payload["sid"],
            "user_id": user_id,
            "revoked_at": None,
            "expires_at": {"$gt": now},
        }
    )
    if session is None:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED"})
    last_seen_at = session["last_seen_at"]
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=UTC)
    idle_after = now - timedelta(minutes=settings.session_idle_minutes)
    if last_seen_at < idle_after:
        await database["sessions"].update_one(
            {"_id": session["_id"]}, {"$set": {"revoked_at": now}}
        )
        raise HTTPException(status_code=401, detail={"code": "SESSION_EXPIRED"})

    user = await database["users"].find_one({"_id": user_id, "is_active": True})
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED"})
    if user.get("must_change_password") and not allow_password_change:
        raise HTTPException(status_code=403, detail={"code": "PASSWORD_CHANGE_REQUIRED"})

    if request.method in _MUTATING_METHODS:
        csrf = request.headers.get("X-CSRF-Token", "")
        provided_hash = hash_csrf_token(csrf) if csrf else ""
        if not hmac.compare_digest(provided_hash, session["csrf_token_hash"]):
            raise HTTPException(status_code=403, detail={"code": "CSRF_INVALID"})

    await database["sessions"].update_one(
        {"_id": session["_id"]}, {"$set": {"last_seen_at": now}}
    )
    return {"user": user, "session": session}


async def get_current_user(
    request: Request,
    database: DatabaseDependency,
) -> dict[str, Any]:
    return await _load_session_user(request, database, allow_password_change=False)


async def get_current_user_for_password_change(
    request: Request,
    database: DatabaseDependency,
) -> dict[str, Any]:
    return await _load_session_user(request, database, allow_password_change=True)


CurrentUserDependency = Annotated[dict[str, Any], Depends(get_current_user)]
PasswordChangeUserDependency = Annotated[
    dict[str, Any], Depends(get_current_user_for_password_change)
]


async def require_any_admin(
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    role = await database["user_project_roles"].find_one(
        {"user_id": current["user"]["_id"], "role": "ADMIN"}
    )
    if role is None:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN"})
    return current


AdminDependency = Annotated[dict[str, Any], Depends(require_any_admin)]


async def require_project_admin(
    project_id: str,
    current: CurrentUserDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    role = await database["user_project_roles"].find_one(
        {
            "user_id": current["user"]["_id"],
            "project_id": project_id,
            "role": "ADMIN",
        }
    )
    if role is None:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN"})
    return current


ProjectAdminDependency = Annotated[dict[str, Any], Depends(require_project_admin)]
