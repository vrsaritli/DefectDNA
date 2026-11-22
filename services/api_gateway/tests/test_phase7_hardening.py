import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import Settings, settings
from libs.common.observability import production_problems
from libs.common.ops import evaluate_alerts, overall_status
from libs.common.service_auth import create_service_token
from services.ai_knowledge_service.app.main import app as aik_app
from services.api_gateway.app import api as gateway_api
from services.api_gateway.app.auth.dependencies import get_database
from services.api_gateway.app.auth.passwords import hash_password
from services.api_gateway.app.auth.repository import ensure_auth_indexes
from services.api_gateway.app.main import app
from services.ingestion_service.app.main import app as ingestion_app
from services.trends_service.app.main import app as trends_app

PASSWORD = "Strong-Password-123!"


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any]]:
    """Gateway wired to the real internal apps (in-process) over one mock database."""
    mongo = AsyncMongoMockClient(tz_aware=True)
    database = mongo[settings.mongodb_db]
    asyncio.run(ensure_auth_indexes(database))
    monkeypatch.setattr(settings, "session_signing_key", "test-session-signing-key-" * 3)
    monkeypatch.setattr(settings, "service_token_secret", "phase7-service-secret-" * 2)
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 1_000)
    internal = {
        "INGESTION": TestClient(ingestion_app),
        "AI_KNOWLEDGE": TestClient(aik_app),
        "TRENDS": TestClient(trends_app),
    }
    for client in internal.values():
        client.__enter__()
        client.app.state.mongo_client = mongo  # type: ignore[attr-defined]

    def make_caller(name: str) -> Any:
        async def call(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            token = create_service_token(settings.service_token_secret, "api-gateway")
            response = internal[name].request(
                method,
                path,
                params=kwargs.get("params"),
                json=kwargs.get("json_body"),
                headers={"Authorization": f"Bearer {token}", **(kwargs.get("headers") or {})},
            )
            payload = response.json()
            if response.is_error:
                raise gateway_api.HTTPException(response.status_code, detail=payload.get("detail"))
            return response.status_code, payload

        return call

    monkeypatch.setattr(gateway_api, "_call_ingestion", make_caller("INGESTION"))
    monkeypatch.setattr(gateway_api, "_call_ai_knowledge", make_caller("AI_KNOWLEDGE"))
    monkeypatch.setattr(gateway_api, "_call_trends", make_caller("TRENDS"))
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app, base_url="https://testserver") as client:
        yield client, database
    app.dependency_overrides.pop(get_database, None)
    for client in internal.values():
        client.__exit__(None, None, None)
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


def test_delete_project_data_removes_data_in_every_service(stack: tuple[TestClient, Any]) -> None:
    client, database = stack
    _user(database, "admin@example.test", "ADMIN")
    _user(database, "lead@example.test", "QA_LEAD")

    async def seed() -> None:
        for name in ("defects", "rca_analyses", "knowledge_entries", "trend_facts"):
            await database[name].insert_many(
                [{"project_id": "project-a", "defect_id": 1, "ado_id": 1},
                 {"project_id": "project-b", "defect_id": 1, "ado_id": 1}]
            )

    asyncio.run(seed())
    lead = _login(client, "lead@example.test")
    body = {"confirm_project_id": "project-a"}
    assert client.request("DELETE", "/api/v1/projects/project-a/data", json=body, headers=lead).status_code == 403

    admin = _login(client, "admin@example.test")
    wrong = client.request(
        "DELETE", "/api/v1/projects/project-a/data", json={"confirm_project_id": "x-y"}, headers=admin
    )
    assert wrong.json()["error"]["code"] == "CONFIRMATION_MISMATCH"

    started = client.request("DELETE", "/api/v1/projects/project-a/data", json=body, headers=admin)
    assert started.status_code == 202
    job = client.get(f"/api/v1/jobs/{started.json()['job_id']}").json()
    assert job["status"] == "SUCCEEDED"
    assert job["progress"] == {"total": 3, "done": 3, "failed": 0}
    for name in ("defects", "rca_analyses", "knowledge_entries", "trend_facts"):
        assert asyncio.run(database[name].count_documents({"project_id": "project-a"})) == 0
        assert asyncio.run(database[name].count_documents({"project_id": "project-b"})) == 1
    assert asyncio.run(database["audit_log"].count_documents({"action": "PROJECT_DATA_DELETED"})) == 1


def test_correlation_id_quota_and_ops(
    stack: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, database = stack
    _user(database, "admin@example.test", "ADMIN")
    admin = _login(client, "admin@example.test")

    echoed = client.get("/api/v1/me", headers={"X-Correlation-ID": "req-12345678"})
    assert echoed.headers["X-Correlation-ID"] == "req-12345678"
    generated = client.get("/api/v1/me", headers={"X-Correlation-ID": "bad id!"})
    assert len(generated.headers["X-Correlation-ID"]) == 32

    monkeypatch.setattr(settings, "upload_quota_per_hour", 1)
    upload = {"file": ("d.csv", b"ID,Title\n1,A\n", "text/csv")}
    first = client.post("/api/v1/projects/project-a/imports", files=upload, headers=admin)
    assert first.status_code != 429  # quota is checked before the call to ingestion
    limited = client.post("/api/v1/projects/project-a/imports", files=upload, headers=admin)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "QUOTA_EXCEEDED"

    ops = client.get("/api/v1/projects/project-a/ops").json()
    assert ops["status"] in {"ok", "warning", "critical"}
    assert ops["metrics"]["usage_today"]["llm_cap"] == settings.llm_daily_call_cap
    assert "auth" in ops["metrics"]


def test_alert_rules_and_production_guard() -> None:
    alerts = evaluate_alerts(
        {
            "jobs": {"stuck": 1, "failed": 2},
            "analysis": {"analyzed": 10, "needs_manual_rate": 0.5, "provider_errors": 6},
            "usage_today": {"llm_calls": 450, "llm_cap": 500, "embed_calls": 0, "embed_cap": 10},
            "knowledge": {"rag_enabled": True, "pending_embedding": 3},
            "auth": {"lockouts": 1, "login_failures": 2},
        }
    )
    codes = {alert.code: alert.severity for alert in alerts}
    assert codes["JOBS_STUCK"] == "critical"
    assert codes["PROVIDER_ERRORS"] == "critical"
    assert codes["LLM_CAP_NEAR"] == "warning"
    assert "EMBEDDINGS_PENDING" in codes and "NEEDS_MANUAL_RATE_HIGH" in codes
    assert overall_status(alerts) == "critical"
    assert overall_status(evaluate_alerts({})) == "ok"

    unsafe = Settings(
        env="prod",
        mongodb_uri="mongodb://x",
        service_token_secret="replace-with-a-long-random-secret-value",
        session_signing_key="short",
        session_cookie_secure=False,
    )
    problems = production_problems(unsafe, needs_session=True)
    assert len(problems) == 3
    assert production_problems(Settings(env="dev"), needs_session=True) == []
