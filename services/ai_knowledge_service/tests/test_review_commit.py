import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import settings
from libs.common.service_auth import create_service_token
from libs.common.taxonomy import starter_taxonomy
from libs.common.trends import TREND_FACT_JOB, TrendFact
from services.ai_knowledge_service.app.main import app

TAXONOMY = starter_taxonomy().to_payload()


@pytest.fixture
def aik(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, dict[str, str]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    monkeypatch.setattr(settings, "service_token_secret", "aik-review-secret-" * 3)
    headers = {
        "Authorization": f"Bearer {create_service_token(settings.service_token_secret, 'api-gateway')}"
    }
    with TestClient(app) as client:
        app.state.mongo_client = mongo
        yield client, mongo, headers
    app.state.mongo_client = None
    mongo.close()


def _insert_rca(mongo: Any, defect_id: int, *, proposed: bool = True) -> str:
    async def insert() -> str:
        result = await mongo["defectdna"]["rca_analyses"].insert_one(
            {
                "project_id": "project-a",
                "defect_id": defect_id,
                "version": 1,
                "status": "AI_PROPOSED" if proposed else "NEEDS_MANUAL_RCA",
                "proposed": {
                    "root_cause_code": "CODE",
                    "sdlc_phase_code": "DEVELOPMENT",
                    "testing_gap_code": "NO_TEST_CASE",
                    "other_proposal": None,
                    "confidence": 0.8,
                    "rationale": "Because.",
                }
                if proposed
                else None,
                "final": None,
                "taxonomy_version": 1,
                "defect_snapshot": {
                    "title": "T",
                    "module": "Shop\\Web",
                    "release": "R1",
                    "severity": "2 - High",
                    "closed_month": "2026-10",
                },
                "created_at": datetime.now(UTC),
            }
        )
        return str(result.inserted_id)

    return asyncio.run(insert())


def _review(client: TestClient, headers: dict[str, str], rca_id: str, **body: Any) -> Any:
    return client.post(
        f"/internal/rcas/{rca_id}/review",
        json={"reviewer_id": "65a000000000000000000001", "taxonomy": TAXONOMY, **body},
        headers=headers,
    )


def test_approve_emits_trend_fact_and_blocks_silent_rereview(
    aik: tuple[TestClient, Any, dict[str, str]],
) -> None:
    client, mongo, headers = aik
    rca_id = _insert_rca(mongo, 11)

    approved = _review(client, headers, rca_id, decision="APPROVE")
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    job = asyncio.run(mongo["defectdna"]["jobs"].find_one({"type": TREND_FACT_JOB}))
    fact = TrendFact.model_validate(job["payload"]["fact"])
    assert (fact.defect_id, fact.root_cause_code, fact.month) == (11, "CODE", "2026-10")
    assert fact.ai_root_cause_code == "CODE"

    again = _review(client, headers, rca_id, decision="APPROVE")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ALREADY_REVIEWED"

    reopened = _review(
        client,
        headers,
        rca_id,
        decision="CORRECT",
        reopen=True,
        final={
            "root_cause_code": "DATA",
            "sdlc_phase_code": "OPERATIONS",
            "testing_gap_code": "TEST_DATA_GAP",
        },
        note="Seed data, not code.",
    )
    assert reopened.json()["status"] == "CORRECTED"
    assert asyncio.run(mongo["defectdna"]["jobs"].count_documents({"type": TREND_FACT_JOB})) == 2

    facts = client.get("/internal/projects/project-a/reviewed-facts", headers=headers).json()["facts"]
    assert len(facts) == 1
    assert facts[0]["root_cause_code"] == "DATA"
    assert facts[0]["status"] == "CORRECTED"


def test_correct_validates_codes_and_manual_rca_cannot_be_approved(
    aik: tuple[TestClient, Any, dict[str, str]],
) -> None:
    client, mongo, headers = aik
    manual_id = _insert_rca(mongo, 12, proposed=False)
    assert _review(client, headers, manual_id, decision="APPROVE").status_code == 422

    bad = _review(
        client,
        headers,
        manual_id,
        decision="CORRECT",
        final={"root_cause_code": "NOPE", "sdlc_phase_code": "DESIGN", "testing_gap_code": "NO_TEST_CASE"},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "UNKNOWN_TAXONOMY_CODE"

    same_as_ai = _insert_rca(mongo, 13)
    result = _review(
        client,
        headers,
        same_as_ai,
        decision="CORRECT",
        final={"root_cause_code": "CODE", "sdlc_phase_code": "DEVELOPMENT", "testing_gap_code": "NO_TEST_CASE"},
    )
    assert result.json()["status"] == "APPROVED"  # unchanged codes count as agreement
