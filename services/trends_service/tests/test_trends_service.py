import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import settings
from libs.common.queue import MongoJobQueue
from libs.common.service_auth import create_service_token
from libs.common.trends import TREND_FACT_JOB
from services.trends_service.app.main import app
from services.trends_service.app.worker import process_trend_fact_job


def _fact(defect_id: int, **overrides: Any) -> dict[str, Any]:
    fact = {
        "project_id": "project-a",
        "defect_id": defect_id,
        "rca_id": f"rca-{defect_id}",
        "status": "APPROVED",
        "module": "Shop\\Web\\Checkout",
        "release": "Release 1",
        "month": "2026-10",
        "severity": "2 - High",
        "root_cause_code": "CODE",
        "sdlc_phase_code": "DEVELOPMENT",
        "testing_gap_code": "NO_TEST_CASE",
    }
    fact.update(overrides)
    return fact


@pytest.fixture
def trends(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, dict[str, str]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    monkeypatch.setattr(settings, "service_token_secret", "trends-test-secret-" * 3)
    headers = {
        "Authorization": f"Bearer {create_service_token(settings.service_token_secret, 'api-gateway')}"
    }
    with TestClient(app) as client:
        app.state.mongo_client = mongo
        yield client, mongo, headers
    app.state.mongo_client = None
    mongo.close()


def test_worker_upserts_one_fact_per_defect(trends: tuple[TestClient, Any, dict[str, str]]) -> None:
    client, mongo, headers = trends
    database = mongo["defectdna"]
    queue = MongoJobQueue(database)

    async def run() -> None:
        for fact in (_fact(1), _fact(1, root_cause_code="DATA", status="CORRECTED"), _fact(2)):
            await queue.enqueue(TREND_FACT_JOB, {"project_id": "project-a", "fact": fact})
            job = await queue.claim(TREND_FACT_JOB, "w")
            assert job is not None
            assert await process_trend_fact_job(job, database=database, queue=queue, owner="w")
        await queue.enqueue(TREND_FACT_JOB, {"project_id": "project-a", "fact": _fact(3)})
        added = await queue.claim(TREND_FACT_JOB, "w")
        assert added is not None
        await process_trend_fact_job(added, database=database, queue=queue, owner="w")
        await queue.enqueue(
            TREND_FACT_JOB,
            {"project_id": "project-a", "remove": {"project_id": "project-a", "defect_id": 3}},
        )
        removal = await queue.claim(TREND_FACT_JOB, "w")
        assert removal is not None
        assert await process_trend_fact_job(removal, database=database, queue=queue, owner="w")
        await queue.enqueue(TREND_FACT_JOB, {"project_id": "project-a", "fact": {"bad": 1}})
        bad = await queue.claim(TREND_FACT_JOB, "w")
        assert bad is not None
        assert not await process_trend_fact_job(bad, database=database, queue=queue, owner="w")

    asyncio.run(run())
    assert asyncio.run(database["trend_facts"].count_documents({})) == 2
    summary = client.get("/internal/projects/project-a/trends/summary", headers=headers).json()
    assert summary["total"] == 2
    assert summary["corrected"] == 1
    assert summary["ai_agreement_rate"] == 0.5
    assert {item["key"]: item["count"] for item in summary["by_root_cause"]} == {"DATA": 1, "CODE": 1}


def test_grouping_filters_and_rebuild(trends: tuple[TestClient, Any, dict[str, str]]) -> None:
    client, _, headers = trends
    facts = [
        _fact(1),
        _fact(2, month="2026-11"),
        _fact(3, module="Shop\\API\\Payments", root_cause_code="DATA"),
        _fact(4, module="Shop\\API\\Payments", month="2026-11", release="Release 2"),
    ]
    rebuilt = client.put(
        "/internal/projects/project-a/trend-facts", json={"facts": facts}, headers=headers
    )
    assert rebuilt.json()["facts"] == 4

    by_module_month = client.get(
        "/internal/projects/project-a/trends?group_by=module,month", headers=headers
    ).json()["rows"]
    assert sum(row["total"] for row in by_module_month) == 4
    assert [row["month"] for row in by_module_month] == sorted(row["month"] for row in by_module_month)
    payments_oct = next(
        row
        for row in by_module_month
        if row["module"] == "Shop\\API\\Payments" and row["month"] == "2026-10"
    )
    assert payments_oct["by_root_cause"] == {"DATA": 1}

    filtered = client.get(
        "/internal/projects/project-a/trends?group_by=release&month_from=2026-11",
        headers=headers,
    ).json()["rows"]
    assert {row["release"]: row["total"] for row in filtered} == {"Release 1": 1, "Release 2": 1}

    # Rebuild is an exact replacement: dropped facts disappear.
    client.put(
        "/internal/projects/project-a/trend-facts", json={"facts": facts[:1]}, headers=headers
    )
    assert client.get("/internal/projects/project-a/trends/summary", headers=headers).json()["total"] == 1


def test_rejects_bad_input_and_missing_token(trends: tuple[TestClient, Any, dict[str, str]]) -> None:
    client, _, headers = trends
    assert client.get("/internal/projects/project-a/trends").status_code == 401
    assert (
        client.get("/internal/projects/project-a/trends?group_by=title", headers=headers).status_code
        == 422
    )
    mismatch = client.put(
        "/internal/projects/project-a/trend-facts",
        json={"facts": [_fact(1, project_id="project-b")]},
        headers=headers,
    )
    assert mismatch.status_code == 422
