from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import Settings
from libs.common.database import ping_database
from libs.common.logging import configure_logging

settings = Settings()
configure_logging("api-gateway")
mongo_client: AsyncIOMotorClient[dict[str, Any]] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global mongo_client
    if settings.mongodb_uri:
        mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
    yield
    if mongo_client:
        mongo_client.close()


app = FastAPI(title="DefectDNA API Gateway", lifespan=lifespan)


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

