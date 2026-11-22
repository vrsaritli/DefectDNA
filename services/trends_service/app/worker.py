"""trends-service worker: turns ``trend_fact`` jobs into trend_facts documents.

Run with:  python -m services.trends_service.app.worker
"""

import asyncio
import os
import socket
from typing import Any

import structlog
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError

from libs.common.config import Settings
from libs.common.database import create_mongo_client
from libs.common.logging import configure_logging
from libs.common.queue import JobQueue, MongoJobQueue
from libs.common.trends import TREND_FACT_JOB, TrendFact
from services.trends_service.app.repository import ensure_trend_indexes, upsert_fact

logger = structlog.get_logger()


async def process_trend_fact_job(
    job: dict[str, Any],
    *,
    database: AsyncIOMotorDatabase[dict[str, Any]],
    queue: JobQueue,
    owner: str,
) -> bool:
    job_id = str(job["_id"])
    try:
        fact = TrendFact.model_validate(job.get("payload", {}).get("fact", {}))
    except ValidationError:
        await queue.fail(job_id, owner, "INVALID_TREND_FACT")
        logger.error("trend_fact_invalid", job_id=job_id)
        return False
    await upsert_fact(database, fact)
    await queue.complete(job_id, owner)
    logger.info(
        "trend_fact_upserted",
        job_id=job_id,
        project_id=fact.project_id,
        defect_id=fact.defect_id,
        status=fact.status,
    )
    return True


async def run_worker(poll_seconds: float = 2.0) -> None:
    settings = Settings()
    configure_logging("trends-worker")
    client = create_mongo_client(settings.mongodb_uri)
    database = client[settings.mongodb_db]
    queue = MongoJobQueue(database)
    await queue.ensure_indexes()
    await ensure_trend_indexes(database)
    owner = os.environ.get("WORKER_ID", f"trends-worker-{socket.gethostname()}-{os.getpid()}")
    logger.info("trends_worker_started", worker_id=owner)
    try:
        while True:
            job = await queue.claim(TREND_FACT_JOB, owner, lease_seconds=60)
            if job is None:
                await asyncio.sleep(poll_seconds)
                continue
            try:
                await process_trend_fact_job(job, database=database, queue=queue, owner=owner)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                logger.error("trend_fact_failed", job_id=str(job["_id"]), error=type(exc).__name__)
                await queue.fail(str(job["_id"]), owner, type(exc).__name__)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
