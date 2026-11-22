from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import Settings
from libs.common.database import create_mongo_client, ping_database
from libs.common.logging import configure_logging
from services.ingestion_service.app.api import router as ingestion_router

settings = Settings()
configure_logging("ingestion-service")
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
    app.state.mongo_client = None


app = FastAPI(title="DefectDNA Ingestion Service", lifespan=lifespan)
app.include_router(ingestion_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ingestion-service"}


@app.get("/health/ready")
async def readiness() -> dict[str, str]:
    if mongo_client is None:
        return {"status": "degraded", "database": "not_configured"}
    if await ping_database(mongo_client[settings.mongodb_db]):
        return {"status": "ready", "database": "ok"}
    return {"status": "degraded", "database": "unreachable"}
