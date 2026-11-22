import asyncio
import os

from .config import Settings
from .database import create_mongo_client
from .queue import MongoJobQueue


async def run_foundation_worker(poll_seconds: float = 2.0) -> None:
    settings = Settings()
    client = create_mongo_client(settings.mongodb_uri)
    queue = MongoJobQueue(client[settings.mongodb_db])
    await queue.ensure_indexes()
    worker_id = os.environ.get("WORKER_ID", "foundation-worker")

    while True:
        job = await queue.claim("FOUNDATION_TEST", worker_id)
        if job:
            await queue.complete(str(job["_id"]), worker_id)
        await asyncio.sleep(poll_seconds)
