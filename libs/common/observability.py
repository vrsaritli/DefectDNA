"""Correlation IDs, request logs and production safety checks shared by all services.

The gateway creates (or accepts) an ``X-Correlation-ID`` per request, binds it to every
log line, returns it to the caller and forwards it to internal services, which bind the
same ID. Request logs carry method, route, status and duration only: no bodies, query
strings, cookies or tokens.
"""

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response

from libs.common.config import Settings

CORRELATION_HEADER = "X-Correlation-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_QUIET_PATHS = frozenset({"/health", "/health/ready"})
logger = structlog.get_logger()


def current_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return str(value) if value else None


def install_request_context(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(CORRELATION_HEADER, "")
        correlation_id = incoming if _VALID_ID.fullmatch(incoming) else uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        tokens = structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            if request.url.path not in _QUIET_PATHS:
                route = request.scope.get("route")
                logger.info(
                    "http_request",
                    method=request.method,
                    route=getattr(route, "path", None) or "unmatched",
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            structlog.contextvars.reset_contextvars(**tokens)


def is_production(settings: Settings) -> bool:
    return settings.env.strip().casefold() in {"prod", "production"}


_PLACEHOLDERS = ("replace-with", "change-me", "changeme", "example")


def production_problems(settings: Settings, *, needs_session: bool) -> list[str]:
    """Configuration that must never reach production (empty list = OK)."""
    if not is_production(settings):
        return []
    problems = []
    if not settings.mongodb_uri:
        problems.append("MONGODB_URI is not set")
    secrets: dict[str, str] = {"SERVICE_TOKEN_SECRET": settings.service_token_secret}
    if needs_session:
        secrets["SESSION_SIGNING_KEY"] = settings.session_signing_key
        if not settings.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true in production")
    for name, value in secrets.items():
        if len(value.encode("utf-8")) < 32:
            problems.append(f"{name} must be at least 32 bytes")
        elif any(marker in value.casefold() for marker in _PLACEHOLDERS):
            problems.append(f"{name} still holds a placeholder value")
    return problems


def enforce_production_settings(settings: Settings, *, needs_session: bool = False) -> None:
    problems = production_problems(settings, needs_session=needs_session)
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


def docs_urls(settings: Settings) -> dict[str, Any]:
    """OpenAPI/Swagger pages only outside production (OWASP API9: inventory exposure)."""
    if is_production(settings):
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}
