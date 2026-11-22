from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import Settings
from libs.common.database import create_mongo_client, ping_database
from libs.common.logging import configure_logging
from services.ai_knowledge_service.app.api import router as aik_router

settings = Settings()
configure_logging("ai-knowledge-service")
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


app = FastAPI(title="DefectDNA AI Knowledge Service", lifespan=lifespan)
app.include_router(aik_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-knowledge-service"}


@app.get("/health/ready")
async def readiness() -> dict[str, str]:
    if mongo_client is None:
        return {"status": "degraded", "database": "not_configured"}
    if await ping_database(mongo_client[settings.mongodb_db]):
        return {"status": "ready", "database": "ok"}
    return {"status": "degraded", "database": "unreachable"}
