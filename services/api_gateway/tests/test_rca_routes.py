import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import settings
from services.api_gateway.app import api as gateway_api
from services.api_gateway.app.auth.dependencies import get_database
from services.api_gateway.app.auth.passwords import hash_password
from services.api_gateway.app.auth.repository import ensure_auth_indexes
from services.api_gateway.app.main import app
from services.api_gateway.app.taxonomy import ensure_taxonomy_indexes

PASSWORD = "Strong-Password-123!"


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, dict[str, Any]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    database = mongo["defectdna_rca_test"]
    asyncio.run(ensure_auth_indexes(database))
    asyncio.run(ensure_taxonomy_indexes(database))
    monkeypatch.setattr(settings, "session_signing_key", "test-session-signing-key-" * 3)
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 1_000)
    monkeypatch.setattr(settings, "env", "dev")
    calls: dict[str, Any] = {"aik": [], "ingestion": []}

    rca_a = {
        "id": "6661b2000000000000000001",
        "project_id": "project-a",
        "defect_id": 48213,
        "version": 1,
        "status": "AI_PROPOSED",
        "proposed": {
            "root_cause_code": "REQUIREMENTS",
            "sdlc_phase_code": "REQUIREMENTS",
            "testing_gap_code": "NO_TEST_CASE",
            "other_proposal": None,
            "confidence": 0.82,
            "rationale": "Scenario was never specified.",
        },
        "taxonomy_version": 1,
        "defect_snapshot": {"title": "Order total wrong", "module": "Checkout"},
        "model": {"llm": "openai/gpt-oss-120b", "prompt_version": "rca-v1"},
    }
    rca_b = {**rca_a, "id": "6661b2000000000000000002", "project_id": "project-b"}

    async def fake_aik(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls["aik"].append({"method": method, "path": path, **kwargs})
        if path.endswith("/analysis-state"):
            return 200, {"analyzed": [1, 2], "reviewed": [2]}
        if path.endswith("/analyses"):
            body = kwargs["json_body"]
            return 202, {
                "job_id": "6660a1000000000000000001",
                "queued": len(body["defects"]),
                "skipped_reviewed": 0,
                "skipped_already_analyzed": 0,
                "taxonomy_version": body["taxonomy"]["version"],
            }
        if path.endswith("/rcas"):
            return 200, {"page": 1, "page_size": 50, "total": 1, "rcas": [rca_a]}
        if path.endswith(rca_a["id"]):
            return 200, {"rca": rca_a}
        if path.endswith(rca_b["id"]):
            return 200, {"rca": rca_b}
        if path.endswith("/rca"):
            return 200, {"rca": rca_a}
        raise AssertionError(path)

    async def fake_ingestion(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls["ingestion"].append({"method": method, "path": path, **kwargs})
        if path.endswith("/for-analysis"):
            return 200, {
                "defects": [{"ado_id": 3, "title": "Third defect", "state": "Closed"}],
                "total_matching": 5,
            }
        return 200, {"defect": {"ado_id": 48213, "title": "Order total wrong"}, "rca": None}

    monkeypatch.setattr(gateway_api, "_call_ai_knowledge", fake_aik)
    monkeypatch.setattr(gateway_api, "_call_ingestion", fake_ingestion)
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app, base_url="https://testserver") as client:
        yield client, database, calls
    app.dependency_overrides.pop(get_database, None)
    mongo.close()


def _user(database: Any, username: str, roles: dict[str, str]) -> None:
    async def insert() -> None:
        now = datetime.now(UTC)
        result = await database["users"].insert_one(
            {
                "username": username,
                "display_name": username,
                "password_hash": hash_password(PASSWORD, username),
                "must_change_password": False,
                "is_active": True,
                "failed_login_count": 0,
                "locked_until": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        for project_id, role in roles.items():
            await database["projects"].update_one(
                {"_id": project_id},
                {"$setOnInsert": {"name": project_id, "created_at": now}},
                upsert=True,
            )
            await database["user_project_roles"].insert_one(
                {"user_id": result.inserted_id, "project_id": project_id, "role": role}
            )

    asyncio.run(insert())


def _login(client: TestClient, username: str) -> dict[str, str]:
    client.cookies.clear()
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_taxonomy_starts_with_starter_and_versions_on_update(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, _ = gateway
    _user(database, "viewer@example.test", {"project-a": "VIEWER"})
    _user(database, "lead@example.test", {"project-a": "QA_LEAD"})

    viewer = _login(client, "viewer@example.test")
    current = client.get("/api/v1/projects/project-a/taxonomy")
    assert current.status_code == 200
    taxonomy = current.json()
    assert taxonomy["version"] == 1
    assert "TEST_GAP" in [item["code"] for item in taxonomy["root_causes"]]
    body = {key: taxonomy[key] for key in ("root_causes", "sdlc_phases", "testing_gaps")}
    assert client.put("/api/v1/projects/project-a/taxonomy", json=body, headers=viewer).status_code == 403

    lead = _login(client, "lead@example.test")
    body["root_causes"].append({"code": "ACCESSIBILITY", "label": "Accessibility"})
    updated = client.put("/api/v1/projects/project-a/taxonomy", json=body, headers=lead)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert client.get("/api/v1/projects/project-a/taxonomy").json()["version"] == 2
    assert asyncio.run(database["audit_log"].count_documents({"action": "TAXONOMY_UPDATED"})) == 1

    body["root_causes"].append({"code": "ACCESSIBILITY", "label": "Duplicate"})
    duplicate = client.put("/api/v1/projects/project-a/taxonomy", json=body, headers=lead)
    assert duplicate.status_code == 422


def test_analysis_request_roles_selection_and_audit(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, calls = gateway
    _user(database, "viewer@example.test", {"project-a": "VIEWER"})
    _user(database, "lead@example.test", {"project-a": "QA_LEAD"})

    viewer = _login(client, "viewer@example.test")
    assert client.post("/api/v1/projects/project-a/analyses", json={}, headers=viewer).status_code == 403

    lead = _login(client, "lead@example.test")
    response = client.post("/api/v1/projects/project-a/analyses", json={}, headers=lead)
    assert response.status_code == 202
    assert response.json()["queued"] == 1
    assert response.json()["remaining"] == 4

    selection = calls["ingestion"][-1]
    assert selection["json_body"]["exclude_ids"] == [1, 2]  # already analyzed
    sent = calls["aik"][-1]["json_body"]
    assert sent["taxonomy"]["version"] == 1
    assert sent["defects"][0]["ado_id"] == 3
    assert asyncio.run(database["audit_log"].count_documents({"action": "ANALYSIS_REQUESTED"})) == 1

    client.post(
        "/api/v1/projects/project-a/analyses", json={"reanalyze": True}, headers=lead
    )
    assert calls["ingestion"][-1]["json_body"]["exclude_ids"] == [2]  # only reviewed

    cross = client.post("/api/v1/projects/project-b/analyses", json={}, headers=lead)
    assert cross.status_code == 403


def test_llm_switch_blocks_analysis_and_defaults_off_in_prod(
    gateway: tuple[TestClient, Any, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, _ = gateway
    _user(database, "admin@example.test", {"project-a": "ADMIN"})
    admin = _login(client, "admin@example.test")

    disabled = client.put(
        "/api/v1/projects/project-a/settings",
        json={"llm_analysis_enabled": False},
        headers=admin,
    )
    assert disabled.status_code == 200
    blocked = client.post("/api/v1/projects/project-a/analyses", json={}, headers=admin)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "LLM_ANALYSIS_DISABLED"

    asyncio.run(
        database["projects"].update_one({"_id": "project-a"}, {"$unset": {"settings": ""}})
    )
    monkeypatch.setattr(settings, "env", "prod")
    assert client.get("/api/v1/projects/project-a/settings").json()["llm_analysis_enabled"] is False


def test_rca_views_add_labels_and_block_other_projects(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, _ = gateway
    _user(database, "viewer@example.test", {"project-a": "VIEWER"})
    _login(client, "viewer@example.test")
    client.get("/api/v1/projects/project-a/taxonomy")  # creates taxonomy v1

    listed = client.get("/api/v1/projects/project-a/rcas?status=AI_PROPOSED")
    assert listed.status_code == 200
    rca = listed.json()["rcas"][0]
    assert rca["proposed"]["root_cause"] == {"code": "REQUIREMENTS", "label": "Requirements"}
    assert rca["proposed"]["testing_gap"]["label"] == "No test case"
    assert rca["defect"]["ado_id"] == 48213

    assert client.get("/api/v1/projects/project-a/rcas?status=BOGUS").status_code == 422
    assert client.get("/api/v1/rcas/6661b2000000000000000001").status_code == 200
    assert client.get("/api/v1/rcas/6661b2000000000000000002").status_code == 403

    detail = client.get("/api/v1/projects/project-a/defects/48213")
    assert detail.status_code == 200
    assert detail.json()["rca"]["status"] == "AI_PROPOSED"
