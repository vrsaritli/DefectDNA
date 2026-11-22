import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import settings
from libs.common.taxonomy import starter_taxonomy
from services.api_gateway.app import api as gateway_api
from services.api_gateway.app.auth.dependencies import get_database
from services.api_gateway.app.auth.passwords import hash_password
from services.api_gateway.app.auth.repository import ensure_auth_indexes
from services.api_gateway.app.knowledge_api import map_label
from services.api_gateway.app.main import app
from services.api_gateway.app.taxonomy import ensure_taxonomy_indexes, get_current_taxonomy

PASSWORD = "Strong-Password-123!"
RCA_ID = "6661b2000000000000000009"


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, dict[str, Any]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    database = mongo["defectdna_knowledge_test"]
    asyncio.run(ensure_auth_indexes(database))
    asyncio.run(ensure_taxonomy_indexes(database))
    asyncio.run(get_current_taxonomy(database, "project-a"))
    monkeypatch.setattr(settings, "session_signing_key", "test-session-signing-key-" * 3)
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 1_000)
    calls: dict[str, list[Any]] = {"aik": [], "ingestion": []}
    rca = {
        "id": RCA_ID,
        "project_id": "project-a",
        "defect_id": 9,
        "status": "APPROVED",
        "taxonomy_version": 1,
        "proposed": {"root_cause_code": "CODE"},
        "retrieved_cases": [
            {"ref": "C1", "defect_id": 4, "similarity": 0.8, "root_cause_code": "DATA"}
        ],
        "cited_cases": [{"ref": "C1", "defect_id": 4, "similarity": 0.8, "root_cause_code": "DATA"}],
    }

    async def fake_aik(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls["aik"].append({"method": method, "path": path, **kwargs})
        if path.endswith("/knowledge/seed"):
            return 200, {"seeded": len(kwargs["json_body"]["items"]), "skipped_reviewed": 0, "knowledge_status": "ACTIVE"}
        if path.endswith("/reopen"):
            rca["status"] = "AI_PROPOSED"
            return 200, {"status": "AI_PROPOSED", "defect_id": 9, "previous_status": "APPROVED"}
        if path.endswith("/review"):
            return 200, {"status": "CORRECTED", "defect_id": 9, "knowledge_status": "ACTIVE"}
        if path == f"/internal/rcas/{RCA_ID}":
            return 200, {"rca": rca}
        raise AssertionError(path)

    async def fake_ingestion(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls["ingestion"].append({"method": method, "path": path, **kwargs})
        if path == "/internal/seed-imports":
            return 200, {
                "import_batch_id": "b1",
                "preview": {"rows_total": 4, "rows_valid": 4, "row_errors": []},
                "rca_columns": {"root_cause": "Root Cause (RCA)"},
                "confirmed": [
                    {"defect": {"ado_id": 1, "title": "a"}, "labels": {"root_cause": "Third Party"}},
                    {"defect": {"ado_id": 2, "title": "b"}, "labels": {"root_cause": "Code - Front-end"}},
                    {"defect": {"ado_id": 3, "title": "c"}, "labels": {"root_cause": "Gremlins"}},
                    {
                        "defect": {"ado_id": 4, "title": "d"},
                        "labels": {"root_cause": "test gap", "testing_gap": "Unknown gap"},
                    },
                ],
            }
        return 200, {"defect": {"ado_id": 9, "title": "Card", "description": "desc"}}

    monkeypatch.setattr(gateway_api, "_call_ai_knowledge", fake_aik)
    monkeypatch.setattr(gateway_api, "_call_ingestion", fake_ingestion)
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app, base_url="https://testserver") as client:
        yield client, database, calls
    app.dependency_overrides.pop(get_database, None)
    mongo.close()


def _user(database: Any, username: str, role: str) -> None:
    async def insert() -> None:
        result = await database["users"].insert_one(
            {
                "username": username,
                "display_name": username,
                "password_hash": hash_password(PASSWORD, username),
                "must_change_password": False,
                "is_active": True,
                "failed_login_count": 0,
                "locked_until": None,
                "created_at": datetime.now(UTC),
            }
        )
        await database["projects"].update_one(
            {"_id": "project-a"}, {"$setOnInsert": {"name": "A"}}, upsert=True
        )
        await database["user_project_roles"].insert_one(
            {"user_id": result.inserted_id, "project_id": "project-a", "role": role}
        )

    asyncio.run(insert())


def _login(client: TestClient, username: str) -> dict[str, str]:
    client.cookies.clear()
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_map_label_handles_labels_codes_and_legacy_subcategories() -> None:
    taxonomy = starter_taxonomy()
    assert map_label(taxonomy, "root_causes", "Third Party") == "THIRD_PARTY"
    assert map_label(taxonomy, "root_causes", "test_gap") == "TEST_GAP"
    assert map_label(taxonomy, "root_causes", "Design - Logic") == "DESIGN"
    assert map_label(taxonomy, "root_causes", "Gremlins") is None


def test_seed_import_maps_labels_and_reports_unmapped(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, calls = gateway
    _user(database, "admin@example.test", "ADMIN")
    _user(database, "lead@example.test", "QA_LEAD")
    upload = {"file": ("seed.xlsx", b"PK", "application/octet-stream")}

    lead = _login(client, "lead@example.test")
    assert client.post("/api/v1/projects/project-a/knowledge/seed", files=upload, headers=lead).status_code == 403

    admin = _login(client, "admin@example.test")
    response = client.post("/api/v1/projects/project-a/knowledge/seed", files=upload, headers=admin)
    assert response.status_code == 200
    report = response.json()
    assert report["seeded"] == 3
    assert {item["ado_id"] for item in report["unmapped"]} == {3, 4}
    sent = next(c for c in calls["aik"] if c["path"].endswith("/knowledge/seed"))["json_body"]
    codes = {item["defect"]["ado_id"]: item["final"] for item in sent["items"]}
    assert codes[1]["root_cause_code"] == "THIRD_PARTY"
    assert codes[2]["root_cause_code"] == "CODE"
    assert codes[4] == {"root_cause_code": "TEST_GAP", "sdlc_phase_code": None, "testing_gap_code": None}
    assert asyncio.run(database["audit_log"].count_documents({"action": "KNOWLEDGE_SEEDED"})) == 1


def test_postman_paths_review_with_correction_and_reopen(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, calls = gateway
    _user(database, "lead@example.test", "QA_LEAD")
    lead = _login(client, "lead@example.test")

    detail = client.get(f"/api/v1/rcas/{RCA_ID}").json()
    assert detail["cited_cases"][0]["root_cause"] == {"code": "DATA", "label": "Data"}

    reopened = client.post(f"/api/v1/projects/project-a/rcas/{RCA_ID}/reopen", json={}, headers=lead)
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "AI_PROPOSED"
    assert asyncio.run(database["audit_log"].count_documents({"action": "RCA_REOPENED"})) == 1

    corrected = client.post(
        f"/api/v1/projects/project-a/rcas/{RCA_ID}/review",
        json={
            "action": "CORRECT",
            "correction": {
                "root_cause_code": "DATA",
                "sdlc_phase_code": "INTEGRATION",
                "testing_gap_code": "TEST_DATA_GAP",
                "rationale": "Stale reference data.",
                "confidence": 0.95,
            },
        },
        headers=lead,
    )
    assert corrected.status_code == 200
    sent = next(c for c in calls["aik"] if c["path"].endswith("/review"))["json_body"]
    assert sent["final"] == {
        "root_cause_code": "DATA",
        "sdlc_phase_code": "INTEGRATION",
        "testing_gap_code": "TEST_DATA_GAP",
    }
    assert sent["note"] == "Stale reference data."
    assert sent["defect"]["description"] == "desc"

    wrong_project = client.post(
        f"/api/v1/projects/project-b/rcas/{RCA_ID}/review", json={"action": "APPROVE"}, headers=lead
    )
    assert wrong_project.status_code == 404
