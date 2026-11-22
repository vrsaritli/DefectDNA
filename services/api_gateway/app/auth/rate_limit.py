import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from pymongo import ReturnDocument

from libs.common.config import settings
from services.api_gateway.app.auth.dependencies import DatabaseDependency
from services.api_gateway.app.auth.tokens import verify_session_token


async def enforce_rate_limit(
    request: Request,
    database: DatabaseDependency,
) -> None:
    now = datetime.now(UTC)
    bucket_start = now.replace(second=0, microsecond=0)
    token = request.cookies.get("defectdna_session", "")
    payload = verify_session_token(token, settings.session_signing_key)
    identity = f"user:{payload['sub']}" if payload else f"ip:{request.client.host if request.client else 'unknown'}"
    bucket_key = hashlib.sha256(f"{identity}:{bucket_start.isoformat()}".encode()).hexdigest()
    result = await database["rate_limits"].find_one_and_update(
        {"_id": bucket_key},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {"expires_at": bucket_start + timedelta(days=1)},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if result is not None and result["count"] > settings.rate_limit_requests_per_minute:
        retry_after = 60 - now.second
        raise HTTPException(
            status_code=429,
            detail={"code": "RATE_LIMITED", "retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
