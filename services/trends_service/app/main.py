from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import Settings
from libs.common.database import create_mongo_client, ping_database
from libs.common.logging import configure_logging
from libs.common.observability import (
    docs_urls,
    enforce_production_settings,
    install_request_context,
)
from services.trends_service.app.api import router as trends_router

settings = Settings()
configure_logging("trends-service")
mongo_client: AsyncIOMotorClient[dict[str, Any]] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global mongo_client
    enforce_production_settings(settings)
    if settings.mongodb_uri:
        mongo_client = create_mongo_client(settings.mongodb_uri)
    app.state.mongo_client = mongo_client
    yield
    if mongo_client:
        mongo_client.close()
        mongo_client = None
    app.state.mongo_client = None


app = FastAPI(title="DefectDNA Trends Service", lifespan=lifespan, **docs_urls(settings))
install_request_context(app)
app.include_router(trends_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "trends-service"}


@app.get("/health/ready")
async def readiness() -> dict[str, str]:
    if mongo_client is None:
        return {"status": "degraded", "database": "not_configured"}
    if await ping_database(mongo_client[settings.mongodb_db]):
        return {"status": "ready", "database": "ok"}
    return {"status": "degraded", "database": "unreachable"}
