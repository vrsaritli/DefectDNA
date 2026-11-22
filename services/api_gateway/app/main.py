from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.exceptions import HTTPException, RequestValidationError
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from libs.common.config import settings
from libs.common.database import create_mongo_client, ping_database
from libs.common.logging import configure_logging
from services.api_gateway.app.api import router
from services.api_gateway.app.auth.rate_limit import enforce_rate_limit
from services.api_gateway.app.rca_api import router as rca_router

configure_logging("api-gateway")
mongo_client: AsyncIOMotorClient[dict[str, Any]] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global mongo_client
    if settings.mongodb_uri:
        mongo_client = create_mongo_client(settings.mongodb_uri)
    app.state.mongo_client = mongo_client
    yield
    if mongo_client:
        mongo_client.close()
        mongo_client = None
    app.state.mongo_client = None


app = FastAPI(title="DefectDNA API Gateway", lifespan=lifespan)
app.include_router(router, dependencies=[Depends(enforce_rate_limit)])
app.include_router(rca_router, dependencies=[Depends(enforce_rate_limit)])


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    response.headers["Cache-Control"] = "no-store"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
    code = str(detail.get("code", "HTTP_ERROR"))
    message = str(detail.get("message", code.replace("_", " ").title()))
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "details": [],
        "correlation_id": request.headers.get("X-Correlation-ID"),
    }
    if "retry_after_seconds" in detail:
        error["details"] = [
            {"field": "retry_after_seconds", "issue": str(detail["retry_after_seconds"])}
        ]
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"] if part != "body"),
            "issue": error["msg"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "details": details,
                "correlation_id": request.headers.get("X-Correlation-ID"),
            }
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-gateway"}


@app.get("/health/ready")
async def readiness() -> dict[str, str]:
    if mongo_client is None:
        return {"status": "degraded", "database": "not_configured"}
    database = mongo_client[settings.mongodb_db]
    if await ping_database(database):
        return {"status": "ready", "database": "ok"}
    return {"status": "degraded", "database": "unreachable"}
