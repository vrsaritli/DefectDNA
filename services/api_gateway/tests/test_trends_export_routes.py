import asyncio
import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from openpyxl import load_workbook

from libs.common.config import settings
from services.api_gateway.app import api as gateway_api
from services.api_gateway.app.auth.dependencies import get_database
from services.api_gateway.app.auth.passwords import hash_password
from services.api_gateway.app.auth.repository import ensure_auth_indexes
from services.api_gateway.app.main import app
from services.api_gateway.app.taxonomy import ensure_taxonomy_indexes, get_current_taxonomy
from services.api_gateway.app.trends_api import safe_cell

PASSWORD = "Strong-Password-123!"
RCA_ID = "6661b2000000000000000001"


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, dict[str, Any]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    database = mongo["defectdna_trends_test"]
    asyncio.run(ensure_auth_indexes(database))
    asyncio.run(ensure_taxonomy_indexes(database))
    asyncio.run(get_current_taxonomy(database, "project-a"))
    monkeypatch.setattr(settings, "session_signing_key", "test-session-signing-key-" * 3)
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 1_000)
    calls: dict[str, list[Any]] = {"aik": [], "trends": []}
    rca = {
        "id": RCA_ID,
        "project_id": "project-a",
        "defect_id": 7,
        "version": 1,
        "status": "AI_PROPOSED",
        "taxonomy_version": 1,
        "proposed": {
            "root_cause_code": "CODE",
            "sdlc_phase_code": "DEVELOPMENT",
            "testing_gap_code": "NO_TEST_CASE",
            "confidence": 0.9,
            "rationale": "=HYPERLINK(\"http://evil\")",
        },
        "defect_snapshot": {"title": "+cmd|' /C calc'!A0", "module": "Web", "release": "R1"},
        "model": {"llm": "openai/gpt-oss-120b", "prompt_version": "rca-v1"},
    }

    async def fake_aik(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls["aik"].append({"method": method, "path": path, **kwargs})
        if path == f"/internal/rcas/{RCA_ID}/review":
            rca.update(status="APPROVED", final={**rca["proposed"], "note": None})
            return 200, {"status": "APPROVED", "defect_id": 7, "trend_job_id": "j1"}
        if path == f"/internal/rcas/{RCA_ID}":
            return 200, {"rca": rca}
        if path.endswith("/reviewed-facts"):
            return 200, {"facts": [{"project_id": "project-a", "defect_id": 7}]}
        if path.endswith("/rcas"):
            return 200, {"total": 1, "rcas": [rca]}
        raise AssertionError(path)

    async def fake_trends(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls["trends"].append({"method": method, "path": path, **kwargs})
        if method == "PUT":
            return 200, {"facts": len(kwargs["json_body"]["facts"])}
        if path.endswith("/summary"):
            return 200, {"total": 1, "by_root_cause": [{"key": "CODE", "count": 1}]}
        return 200, {
            "group_by": ["module", "month"],
            "rows": [{"module": "Web", "month": "2026-10", "total": 1, "by_root_cause": {"CODE": 1}}],
        }

    async def fake_ingestion(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return 200, {"total": 1, "defects": [{"ado_id": 7, "title": "@SUM(A1)", "tags": ["a", "b"]}]}

    monkeypatch.setattr(gateway_api, "_call_ai_knowledge", fake_aik)
    monkeypatch.setattr(gateway_api, "_call_trends", fake_trends)
    monkeypatch.setattr(gateway_api, "_call_ingestion", fake_ingestion)
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app, base_url="https://testserver") as client:
        yield client, database, calls
    app.dependency_overrides.pop(get_database, None)
    mongo.close()


def _user(database: Any, username: str, role: str, project_id: str = "project-a") -> None:
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
            }
        )
        await database["projects"].update_one(
            {"_id": project_id}, {"$setOnInsert": {"name": project_id}}, upsert=True
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


def test_only_qa_lead_can_review_and_unknown_codes_are_rejected(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, calls = gateway
    _user(database, "admin@example.test", "ADMIN")
    _user(database, "lead@example.test", "QA_LEAD")

    admin = _login(client, "admin@example.test")
    denied = client.post(f"/api/v1/rcas/{RCA_ID}/review", json={"decision": "APPROVE"}, headers=admin)
    assert denied.status_code == 403

    lead = _login(client, "lead@example.test")
    bad = client.post(
        f"/api/v1/rcas/{RCA_ID}/review",
        json={
            "decision": "CORRECT",
            "final": {"root_cause_code": "NOPE", "sdlc_phase_code": "DESIGN", "testing_gap_code": "NO_TEST_CASE"},
        },
        headers=lead,
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "UNKNOWN_TAXONOMY_CODE"

    approved = client.post(f"/api/v1/rcas/{RCA_ID}/review", json={"action": "APPROVE"}, headers=lead)
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["final"]["root_cause"] == {"code": "CODE", "label": "Code"}
    sent = next(c for c in calls["aik"] if c["path"].endswith("/review"))
    assert sent["json_body"]["taxonomy"]["version"] == 1
    assert asyncio.run(database["audit_log"].count_documents({"action": "RCA_APPROVED"})) == 1


def test_trends_have_labels_and_rebuild_reconciles(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, calls = gateway
    _user(database, "viewer@example.test", "VIEWER")
    _user(database, "lead@example.test", "QA_LEAD")

    viewer = _login(client, "viewer@example.test")
    trends = client.get("/api/v1/projects/project-a/trends?group_by=module,month")
    assert trends.status_code == 200
    assert trends.json()["labels"]["root_cause"]["CODE"] == "Code"
    assert client.get("/api/v1/projects/project-a/trends?group_by=title").status_code == 422
    assert client.get("/api/v1/projects/project-b/trends").status_code == 403
    assert client.post("/api/v1/projects/project-a/trends/rebuild", headers=viewer).status_code == 403

    lead = _login(client, "lead@example.test")
    rebuilt = client.post("/api/v1/projects/project-a/trends/rebuild", headers=lead)
    assert rebuilt.json()["facts"] == 1
    put = next(c for c in calls["trends"] if c["method"] == "PUT")
    assert put["json_body"]["facts"] == [{"project_id": "project-a", "defect_id": 7}]


def test_exports_are_injection_safe_and_audited(
    gateway: tuple[TestClient, Any, dict[str, Any]],
) -> None:
    client, database, _ = gateway
    _user(database, "viewer@example.test", "VIEWER")
    _login(client, "viewer@example.test")

    rcas_csv = client.get("/api/v1/projects/project-a/export?type=rcas&format=csv")
    assert rcas_csv.status_code == 200
    assert "attachment" in rcas_csv.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(rcas_csv.content.decode("utf-8-sig"))))
    header, row = rows[0], rows[1]
    assert row[header.index("title")].startswith("'+cmd")
    assert row[header.index("ai_rationale")].startswith("'=HYPERLINK")
    assert row[header.index("ai_root_cause")] == "Code"

    defects_xlsx = client.get("/api/v1/projects/project-a/export?type=defects&format=xlsx")
    sheet = load_workbook(io.BytesIO(defects_xlsx.content)).active
    assert sheet is not None
    values = [cell.value for cell in sheet[2]]
    assert "'@SUM(A1)" in values
    assert "a; b" in values

    trends_csv = client.get("/api/v1/projects/project-a/export?type=trends&format=csv")
    trend_rows = list(csv.reader(io.StringIO(trends_csv.content.decode("utf-8-sig"))))
    assert trend_rows[0][:3] == ["module", "release", "month"] or trend_rows[0][0] == "module"
    assert "Code" in trend_rows[0]

    assert client.get("/api/v1/projects/project-b/export?type=rcas").status_code == 403
    assert asyncio.run(database["audit_log"].count_documents({"action": "DATA_EXPORTED"})) == 3


def test_safe_cell_neutralises_formula_prefixes() -> None:
    for raw in ("=1+1", "+1", "-1", "@A1", "\tx", "\rx"):
        assert str(safe_cell(raw)).startswith("'")
    assert safe_cell("normal text") == "normal text"
    assert safe_cell(5) == 5
    assert safe_cell(None) == ""
