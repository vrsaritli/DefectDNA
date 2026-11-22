import asyncio
import json
import math
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import Settings, settings
from libs.common.queue import MongoJobQueue
from libs.common.service_auth import create_service_token
from libs.common.taxonomy import starter_taxonomy
from libs.common.trends import TREND_FACT_JOB
from services.ai_knowledge_service.app import knowledge_api
from services.ai_knowledge_service.app.adapters.embedding import (
    CohereEmbeddingAdapter,
    EmbeddingAdapterError,
)
from services.ai_knowledge_service.app.adapters.llm import LLMResponse
from services.ai_knowledge_service.app.analysis import TokenBudget, analyze_defects
from services.ai_knowledge_service.app.knowledge import (
    REEMBED_JOB,
    build_retriever,
)
from services.ai_knowledge_service.app.main import app
from services.ai_knowledge_service.app.worker import process_reembed_job

TAXONOMY = starter_taxonomy()
DIMS = 4


def _vector(text: str) -> list[float]:
    """Deterministic toy embedding: keyword presence -> direction."""
    lowered = text.casefold()
    raw = [
        1.0 if "card" in lowered else 0.0,
        1.0 if "search" in lowered else 0.0,
        1.0 if "email" in lowered else 0.0,
        0.1,
    ]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


class FakeEmbedder:
    model = "fake-embed"
    dimensions = DIMS

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str | None, list[str]]] = []

    async def embed(self, texts: list[str], input_type: Any = None) -> list[list[float]]:
        self.calls.append((input_type, texts))
        if self.fail:
            raise EmbeddingAdapterError("EMBEDDING_UNAVAILABLE")
        return [_vector(text) for text in texts]


@pytest.fixture
def aik(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, dict[str, str]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    monkeypatch.setattr(settings, "service_token_secret", "aik-phase5-secret-" * 3)
    monkeypatch.setattr(settings, "rag_vector_search_mode", "local")
    monkeypatch.setattr(settings, "rag_similarity_threshold", 0.5)
    headers = {
        "Authorization": f"Bearer {create_service_token(settings.service_token_secret, 'api-gateway')}"
    }
    with TestClient(app) as client:
        app.state.mongo_client = mongo
        yield client, mongo, headers
    app.state.mongo_client = None
    mongo.close()


def _use_embedder(monkeypatch: pytest.MonkeyPatch, embedder: Any) -> None:
    monkeypatch.setattr(knowledge_api, "build_embedding_client", lambda _: embedder)


def _insert_rca(mongo: Any, defect_id: int, title: str) -> str:
    async def insert() -> str:
        result = await mongo["defectdna"]["rca_analyses"].insert_one(
            {
                "project_id": "project-a",
                "defect_id": defect_id,
                "version": 1,
                "status": "AI_PROPOSED",
                "proposed": {
                    "root_cause_code": "CODE",
                    "sdlc_phase_code": "DEVELOPMENT",
                    "testing_gap_code": "NO_TEST_CASE",
                },
                "taxonomy_version": 1,
                "defect_snapshot": {"title": title, "module": "Shop\\API", "closed_month": "2026-10"},
                "created_at": datetime.now(UTC),
            }
        )
        return str(result.inserted_id)

    return asyncio.run(insert())


def _review(client: TestClient, headers: dict[str, str], rca_id: str, **body: Any) -> Any:
    return client.post(
        f"/internal/rcas/{rca_id}/review",
        json={
            "reviewer_id": "65a000000000000000000001",
            "taxonomy": TAXONOMY.to_payload(),
            "defect": {"title": "Saved card cannot be deleted", "description": "card stays listed"},
            **body,
        },
        headers=headers,
    )


def test_approval_writes_embedded_knowledge_entry(
    aik: tuple[TestClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo, headers = aik
    embedder = FakeEmbedder()
    _use_embedder(monkeypatch, embedder)
    rca_id = _insert_rca(mongo, 21, "Saved card cannot be deleted")

    response = _review(client, headers, rca_id, decision="APPROVE")
    assert response.status_code == 200
    assert response.json()["knowledge_status"] == "ACTIVE"
    entry = asyncio.run(mongo["defectdna"]["knowledge_entries"].find_one({"source_rca_id": rca_id}))
    assert entry["status"] == "ACTIVE"
    assert entry["embedding_model"] == "fake-embed"
    assert len(entry["embedding"]) == DIMS
    assert "Confirmed root cause: Code" in entry["summary_text"]
    assert embedder.calls[0][0] == "search_document"


def test_embedding_failure_commits_nothing(
    aik: tuple[TestClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo, headers = aik
    _use_embedder(monkeypatch, FakeEmbedder(fail=True))
    rca_id = _insert_rca(mongo, 22, "Saved card cannot be deleted")

    response = _review(client, headers, rca_id, decision="APPROVE")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "EMBEDDING_PROVIDER_ERROR"
    database = mongo["defectdna"]
    rca = asyncio.run(database["rca_analyses"].find_one({"defect_id": 22}))
    assert rca["status"] == "AI_PROPOSED"
    assert asyncio.run(database["knowledge_entries"].count_documents({})) == 0
    assert asyncio.run(database["jobs"].count_documents({"type": TREND_FACT_JOB})) == 0


def test_without_provider_entry_is_pending_then_reembedded(
    aik: tuple[TestClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo, headers = aik
    _use_embedder(monkeypatch, None)
    rca_id = _insert_rca(mongo, 23, "Saved card cannot be deleted")
    assert _review(client, headers, rca_id, decision="APPROVE").json()["knowledge_status"] == "PENDING_EMBEDDING"

    database = mongo["defectdna"]
    queue = MongoJobQueue(database)

    async def run() -> dict[str, Any]:
        await queue.enqueue(REEMBED_JOB, {"project_id": "project-a", "force": False})
        job = await queue.claim(REEMBED_JOB, "w")
        assert job is not None
        await process_reembed_job(job, database=database, queue=queue, embedder=FakeEmbedder(), owner="w")
        entry = await database["knowledge_entries"].find_one({"source_rca_id": rca_id})
        assert entry is not None
        return entry

    entry = asyncio.run(run())
    assert entry["status"] == "ACTIVE"
    assert entry["embedding_model"] == "fake-embed"


def test_reopen_removes_knowledge_and_trend(
    aik: tuple[TestClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo, headers = aik
    _use_embedder(monkeypatch, FakeEmbedder())
    rca_id = _insert_rca(mongo, 24, "Saved card cannot be deleted")
    _review(client, headers, rca_id, decision="APPROVE")

    reopened = client.post(
        f"/internal/rcas/{rca_id}/reopen",
        json={"reviewer_id": "65a000000000000000000001"},
        headers=headers,
    )
    assert reopened.json()["status"] == "AI_PROPOSED"
    assert reopened.json()["knowledge_entries_removed"] == 1
    database = mongo["defectdna"]
    removal = asyncio.run(
        database["jobs"].find_one({"type": TREND_FACT_JOB, "payload.remove": {"$exists": True}})
    )
    assert removal["payload"]["remove"] == {"project_id": "project-a", "defect_id": 24}
    again = client.post(
        f"/internal/rcas/{rca_id}/reopen",
        json={"reviewer_id": "65a000000000000000000001"},
        headers=headers,
    )
    assert again.status_code == 409


def test_seed_import_creates_approved_rcas_and_entries(
    aik: tuple[TestClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, mongo, headers = aik
    _use_embedder(monkeypatch, FakeEmbedder())
    body = {
        "taxonomy": TAXONOMY.to_payload(),
        "reviewer_id": "65a000000000000000000001",
        "items": [
            {
                "defect": {"ado_id": 31, "title": "Search times out", "module": "Web\\Search"},
                "final": {"root_cause_code": "CONFIGURATION"},
            },
            {
                "defect": {"ado_id": 32, "title": "Card surcharge missing", "module": "API\\Payments"},
                "final": {"root_cause_code": "REQUIREMENTS", "sdlc_phase_code": "REQUIREMENTS"},
            },
        ],
    }
    seeded = client.post("/internal/projects/project-a/knowledge/seed", json=body, headers=headers)
    assert seeded.json() == {"seeded": 2, "skipped_reviewed": 0, "knowledge_status": "ACTIVE"}

    database = mongo["defectdna"]
    rca = asyncio.run(database["rca_analyses"].find_one({"defect_id": 31}))
    assert rca["status"] == "APPROVED"
    assert rca["review_source"] == "SEED_IMPORT"
    assert asyncio.run(database["knowledge_entries"].count_documents({"source": "SEED_IMPORT"})) == 2
    job = asyncio.run(database["jobs"].find_one({"type": TREND_FACT_JOB}))
    assert job["payload"]["fact"]["source"] == "SEED_IMPORT"
    assert job["payload"]["fact"]["testing_gap_code"] == "Unknown"

    again = client.post("/internal/projects/project-a/knowledge/seed", json=body, headers=headers)
    assert again.json()["skipped_reviewed"] == 2

    bad = {**body, "items": [{**body["items"][0], "final": {"root_cause_code": "NOPE"}}]}
    assert client.post("/internal/projects/project-a/knowledge/seed", json=bad, headers=headers).status_code == 422

    stats = client.get("/internal/projects/project-a/knowledge/stats", headers=headers).json()
    assert stats["active"] == 2
    assert stats["by_source"] == {"SEED_IMPORT": 2}


async def _seed_entries(database: Any) -> None:
    now = datetime.now(UTC)
    entries = [
        ("project-a", 41, "Saved card cannot be deleted", "CODE"),
        ("project-a", 42, "Search returns stale results", "CONFIGURATION"),
        ("project-b", 43, "Saved card cannot be deleted elsewhere", "DATA"),
    ]
    for project_id, defect_id, title, code in entries:
        await database["knowledge_entries"].insert_one(
            {
                "project_id": project_id,
                "defect_id": defect_id,
                "source_rca_id": f"rca-{defect_id}",
                "status": "ACTIVE",
                "title": title,
                "module": "Shop",
                "summary": title,
                "root_cause_code": code,
                "sdlc_phase_code": "DEVELOPMENT",
                "testing_gap_code": "NO_TEST_CASE",
                "embedding": _vector(title),
                "embedding_model": "fake-embed",
                "created_at": now,
            }
        )


def test_retrieval_is_project_scoped_and_citations_are_subset(
    fake_llm: Any, answer: Callable[..., LLMResponse], defect_factory: Callable[..., dict[str, Any]]
) -> None:
    mongo = AsyncMongoMockClient(tz_aware=True)
    database = mongo["defectdna"]
    asyncio.run(_seed_entries(database))
    rag_settings = Settings(rag_vector_search_mode="local", rag_similarity_threshold=0.5)
    retriever = build_retriever(database, FakeEmbedder(), rag_settings, "project-a")

    defect = defect_factory(41, title="Saved card cannot be deleted", description="card")
    other = defect_factory(50, title="Saved card delete fails", description="card")

    def responder(refs: list[str], call: int) -> LLMResponse:
        if call == 1:  # cite a case that was not retrieved for ADO-50 -> rejected, retried
            return answer(refs, cited_cases=["C3"])
        return answer(refs, cited_cases=["C1"])

    llm = fake_llm(responder)
    outcomes = asyncio.run(
        analyze_defects([defect, other], TAXONOMY, llm, budget=TokenBudget(), retriever=retriever)
    )
    first_prompt = llm.calls[0]["messages"][1]["content"]
    assert "similar_confirmed_cases" in first_prompt
    assert "elsewhere" not in first_prompt  # project-b entry never retrieved

    result = outcomes[50]
    assert result.retrieval_status == "OK"
    assert [case["defect_id"] for case in result.retrieved_cases] == [41]
    assert {c["knowledge_entry_id"] for c in result.cited_cases} <= {
        c["knowledge_entry_id"] for c in result.retrieved_cases
    }
    assert result.llm_attempts == 2
    # A defect never retrieves its own knowledge entry.
    assert all(case["defect_id"] != 41 for case in outcomes[41].retrieved_cases)
    mongo.close()


def test_retrieval_failure_degrades_without_blocking(
    fake_llm: Any, answer: Callable[..., LLMResponse], defect_factory: Callable[..., dict[str, Any]]
) -> None:
    mongo = AsyncMongoMockClient(tz_aware=True)
    database = mongo["defectdna"]
    asyncio.run(_seed_entries(database))
    retriever = build_retriever(database, FakeEmbedder(fail=True), Settings(), "project-a")
    llm = fake_llm(lambda refs, _: answer(refs, cited_cases=[]))
    outcomes = asyncio.run(
        analyze_defects([defect_factory(60)], TAXONOMY, llm, budget=TokenBudget(), retriever=retriever)
    )
    assert outcomes[60].status == "AI_PROPOSED"
    assert outcomes[60].retrieval_status == "EMBEDDING_UNAVAILABLE"
    assert outcomes[60].retrieved_cases == []
    mongo.close()


async def test_cohere_adapter_request_and_dimension_check() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        count = len(seen["body"]["texts"])
        return httpx.Response(200, json={"embeddings": {"float": [[0.1] * DIMS] * count}})

    async def no_sleep(_: float) -> None:
        return None

    adapter = CohereEmbeddingAdapter(
        "key", "embed-english-v3.0", dimensions=DIMS, transport=httpx.MockTransport(handler), sleep=no_sleep
    )
    vectors = await adapter.embed(["a", "b"], "search_query")
    assert len(vectors) == 2
    assert seen["path"] == "/v2/embed"
    assert seen["body"]["input_type"] == "search_query"
    assert seen["body"]["embedding_types"] == ["float"]

    wrong = CohereEmbeddingAdapter(
        "key", "m", dimensions=8, transport=httpx.MockTransport(handler), sleep=no_sleep
    )
    with pytest.raises(EmbeddingAdapterError) as error:
        await wrong.embed(["a"])
    assert error.value.code == "EMBEDDING_DIMENSION_MISMATCH"

    unauthorized = CohereEmbeddingAdapter(
        "key", "m", transport=httpx.MockTransport(lambda _: httpx.Response(401)), sleep=no_sleep
    )
    with pytest.raises(EmbeddingAdapterError) as error:
        await unauthorized.embed(["a"])
    assert error.value.code == "EMBEDDING_AUTH_FAILED"
