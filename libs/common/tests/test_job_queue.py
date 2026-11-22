
import pytest
from mongomock_motor import AsyncMongoMockClient

from libs.common.queue import MongoJobQueue
from libs.common.service_auth import create_service_token, verify_service_token


@pytest.mark.asyncio
async def test_job_claim_and_complete() -> None:
    client = AsyncMongoMockClient()
    queue = MongoJobQueue(client["defectdna"])
    await queue.ensure_indexes()

    job_id = await queue.enqueue("FOUNDATION_TEST", {"sample": True})
    job = await queue.claim("FOUNDATION_TEST", "test-worker")

    assert job is not None
    assert job["_id"] is not None
    assert job["status"] == "CLAIMED"
    assert await queue.complete(str(job["_id"]), "test-worker")

    stored = await queue.get(job_id)
    assert stored is not None
    assert stored["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_lease_allows_expired_job_reclaim() -> None:
    client = AsyncMongoMockClient()
    queue = MongoJobQueue(client["defectdna"])
    await queue.enqueue("FOUNDATION_TEST", {})

    first = await queue.claim("FOUNDATION_TEST", "worker-a", lease_seconds=-1)
    second = await queue.claim("FOUNDATION_TEST", "worker-b")

    assert first is not None
    assert second is not None
    assert second["owner"] == "worker-b"


def test_service_token_round_trip() -> None:
    token = create_service_token("test-secret", "api-gateway", ttl_seconds=60)
    payload = verify_service_token(token, "test-secret")

    assert payload is not None
    assert payload["service"] == "api-gateway"
    assert isinstance(payload["exp"], int)

