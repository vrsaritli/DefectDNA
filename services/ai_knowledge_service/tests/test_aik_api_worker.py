import asyncio
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import Settings, settings
from libs.common.queue import MongoJobQueue
from libs.common.service_auth import create_service_token
from libs.common.taxonomy import starter_taxonomy
from services.ai_knowledge_service.app.adapters.llm import LLMResponse
from services.ai_knowledge_service.app.main import app
from services.ai_knowledge_service.app.worker import JOB_TYPE, process_analysis_job

SECRET_TEXT = "Customer jane.roe@example.com saw ORDER-SECRET-TEXT"


@pytest.fixture
def aik(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, dict[str, str]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    monkeypatch.setattr(settings, "service_token_secret", "aik-test-secret-" * 3)
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    headers = {
        "Authorization": f"Bearer {create_service_token(settings.service_token_secret, 'api-gateway')}"
    }
    with TestClient(app) as client:
        app.state.mongo_client = mongo
        yield client, mongo, headers
    app.state.mongo_client = None
    mongo.close()


def _request(defects: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "taxonomy": starter_taxonomy().to_payload(),
        "defects": defects,
        "requested_by": "65a000000000000000000001",
        **extra,
    }


def _run_job(mongo: Any, llm: Any) -> dict[str, Any]:
    database = mongo["defectdna"]
    queue = MongoJobQueue(database)

    async def run() -> dict[str, Any]:
        job = await queue.claim(JOB_TYPE, "test-worker", lease_seconds=600)
        assert job is not None
        await process_analysis_job(
            job,
            database=database,
            queue=queue,
            llm=llm,
            owner="test-worker",
            settings=Settings(groq_api_key="test-key", llm_daily_call_cap=100),
        )
        stored = await database["jobs"].find_one({"_id": job["_id"]})
        assert stored is not None
        return stored

    return asyncio.run(run())


def test_internal_routes_require_gateway_token(
    aik: tuple[TestClient, Any, dict[str, str]],
    defect_factory: Callable[..., dict[str, Any]],
) -> None:
    client, _, _ = aik
    response = client.post(
        "/internal/projects/project-a/analyses",
        json=_request([defect_factory(1)]),
        headers={"Authorization": "Bearer forged"},
    )
    assert response.status_code == 401
    assert client.get("/internal/projects/project-a/rcas").status_code == 401


def test_job_runs_end_to_end_without_logging_defect_text(
    aik: tuple[TestClient, Any, dict[str, str]],
    fake_llm: Any,
    answer: Callable[..., LLMResponse],
    defect_factory: Callable[..., dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, mongo, headers = aik
    defects = [defect_factory(i, description=SECRET_TEXT) for i in (101, 102, 103)]
    created = client.post(
        "/internal/projects/project-a/analyses", json=_request(defects), headers=headers
    )
    assert created.status_code == 202
    assert created.json()["queued"] == 3

    def responder(refs: list[str], _: int) -> LLMResponse:
        # 103 always gets an invalid code -> NEEDS_MANUAL_RCA after one retry
        good = [ref for ref in refs if ref != "ADO-103"]
        response = answer(good)
        if "ADO-103" in refs:
            bad = answer(["ADO-103"], root_cause_code="BOGUS")
            import json

            merged = json.loads(response.content)["results"] + json.loads(bad.content)["results"]
            response.content = json.dumps({"results": merged})
        return response

    with caplog.at_level(logging.INFO):
        job = _run_job(mongo, fake_llm(responder))

    assert job["status"] == "SUCCEEDED"
    assert "defects" not in job["payload"]
    assert job["payload"]["progress"]["done"] == 3
    assert job["payload"]["progress"]["proposed"] == 2
    assert job["payload"]["progress"]["needs_manual"] == 1
    assert job["payload"]["errors"] == [
        {"item": "ADO-103", "message": "NEEDS_MANUAL_RCA: UNKNOWN_ROOT_CAUSE_CODE"}
    ]
    assert "ORDER-SECRET-TEXT" not in caplog.text
    assert "jane.roe" not in caplog.text
    assert "defect_analyzed" in caplog.text

    listed = client.get("/internal/projects/project-a/rcas", headers=headers).json()
    assert listed["total"] == 3
    statuses = {rca["defect_id"]: rca["status"] for rca in listed["rcas"]}
    assert statuses == {101: "AI_PROPOSED", 102: "AI_PROPOSED", 103: "NEEDS_MANUAL_RCA"}
    proposed = next(rca for rca in listed["rcas"] if rca["defect_id"] == 101)
    assert proposed["proposed"]["root_cause_code"] == "CODE"
    assert proposed["taxonomy_version"] == 1
    assert proposed["model"]["prompt_version"] == "rca-v3"
    assert proposed["retrieval_status"] == "DISABLED"
    assert proposed["scrub_counts"] == {"email": 1}
    assert proposed["llm_batch"]["size"] >= 1

    detail = client.get(f"/internal/rcas/{proposed['id']}", headers=headers)
    assert detail.status_code == 200
    by_defect = client.get("/internal/projects/project-a/defects/103/rca", headers=headers)
    assert by_defect.json()["rca"]["failure_reason"] == "UNKNOWN_ROOT_CAUSE_CODE"
    state = client.get("/internal/projects/project-a/analysis-state", headers=headers).json()
    assert state == {"analyzed": [101, 102, 103], "reviewed": []}


def test_reanalysis_is_idempotent_and_never_overwrites_reviewed(
    aik: tuple[TestClient, Any, dict[str, str]],
    fake_llm: Any,
    answer: Callable[..., LLMResponse],
    defect_factory: Callable[..., dict[str, Any]],
) -> None:
    client, mongo, headers = aik
    defects = [defect_factory(201), defect_factory(202)]
    client.post("/internal/projects/project-a/analyses", json=_request(defects), headers=headers)
    _run_job(mongo, fake_llm(lambda refs, _: answer(refs)))

    database = mongo["defectdna"]
    asyncio.run(
        database["rca_analyses"].update_one(
            {"project_id": "project-a", "defect_id": 201}, {"$set": {"status": "APPROVED"}}
        )
    )

    skipped = client.post(
        "/internal/projects/project-a/analyses", json=_request(defects), headers=headers
    ).json()
    assert skipped["job_id"] is None
    assert skipped["skipped_reviewed"] == 1
    assert skipped["skipped_already_analyzed"] == 1

    again = client.post(
        "/internal/projects/project-a/analyses",
        json=_request(defects, reanalyze=True),
        headers=headers,
    ).json()
    assert again["queued"] == 1
    _run_job(mongo, fake_llm(lambda refs, _: answer(refs, root_cause_code="DATA")))

    docs = asyncio.run(
        database["rca_analyses"].find({"project_id": "project-a"}).to_list(length=None)
    )
    assert len(docs) == 2
    by_id = {doc["defect_id"]: doc for doc in docs}
    assert by_id[201]["status"] == "APPROVED"
    assert by_id[201]["proposed"]["root_cause_code"] == "CODE"
    assert by_id[202]["proposed"]["root_cause_code"] == "DATA"


def test_missing_groq_key_and_oversized_batch_are_rejected(
    aik: tuple[TestClient, Any, dict[str, str]],
    defect_factory: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, headers = aik
    monkeypatch.setattr(settings, "max_analysis_batch", 1)
    too_many = client.post(
        "/internal/projects/project-a/analyses",
        json=_request([defect_factory(1), defect_factory(2)]),
        headers=headers,
    )
    assert too_many.status_code == 422

    monkeypatch.setattr(settings, "groq_api_key", "")
    no_key = client.post(
        "/internal/projects/project-a/analyses",
        json=_request([defect_factory(1)]),
        headers=headers,
    )
    assert no_key.status_code == 503
    assert no_key.json()["detail"]["code"] == "LLM_NOT_CONFIGURED"


def test_daily_cap_leaves_defects_unanalyzed(
    aik: tuple[TestClient, Any, dict[str, str]],
    fake_llm: Any,
    answer: Callable[..., LLMResponse],
    defect_factory: Callable[..., dict[str, Any]],
) -> None:
    client, mongo, headers = aik
    client.post(
        "/internal/projects/project-a/analyses",
        json=_request([defect_factory(301)]),
        headers=headers,
    )
    database = mongo["defectdna"]
    queue = MongoJobQueue(database)

    async def run() -> dict[str, Any]:
        job = await queue.claim(JOB_TYPE, "w", lease_seconds=600)
        assert job is not None
        await process_analysis_job(
            job,
            database=database,
            queue=queue,
            llm=fake_llm(lambda refs, _: answer(refs)),
            owner="w",
            settings=Settings(groq_api_key="k", llm_daily_call_cap=1),
        )
        # second job on the same day hits the cap
        await queue.enqueue(
            JOB_TYPE,
            {
                "project_id": "project-a",
                "taxonomy": starter_taxonomy().to_payload(),
                "defects": [defect_factory(302)],
            },
        )
        job2 = await queue.claim(JOB_TYPE, "w", lease_seconds=600)
        assert job2 is not None
        await process_analysis_job(
            job2,
            database=database,
            queue=queue,
            llm=fake_llm(lambda refs, _: answer(refs)),
            owner="w",
            settings=Settings(groq_api_key="k", llm_daily_call_cap=1),
        )
        stored = await database["jobs"].find_one({"_id": ObjectId(str(job2["_id"]))})
        assert stored is not None
        return stored

    job = asyncio.run(run())
    assert job["payload"]["errors"] == [{"item": "ADO-302", "message": "LLM_DAILY_CAP_REACHED"}]
    count = asyncio.run(database["rca_analyses"].count_documents({"defect_id": 302}))
    assert count == 0
