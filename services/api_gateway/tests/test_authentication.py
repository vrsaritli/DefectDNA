import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import settings
from services.api_gateway.app.auth.dependencies import get_database
from services.api_gateway.app.auth.passwords import hash_password
from services.api_gateway.app.auth.repository import ensure_auth_indexes
from services.api_gateway.app.main import app


@pytest.fixture
def auth_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    database = mongo["defectdna_auth_test"]
    asyncio.run(ensure_auth_indexes(database))
    monkeypatch.setattr(settings, "session_signing_key", "test-session-signing-key-" * 3)
    monkeypatch.setattr(settings, "session_cookie_secure", True)
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 120)
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app, base_url="https://testserver") as client:
        yield client, database
    app.dependency_overrides.pop(get_database, None)
    mongo.close()


def _insert_user(
    database: Any,
    username: str,
    password: str,
    *,
    project_id: str = "project-a",
    role: str = "ADMIN",
    must_change_password: bool = False,
) -> ObjectId:
    now = datetime.now(UTC)

    async def insert() -> ObjectId:
        await database["projects"].update_one(
            {"_id": project_id},
            {
                "$setOnInsert": {
                    "name": project_id,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        result = await database["users"].insert_one(
            {
                "username": username,
                "display_name": username,
                "password_hash": hash_password(password, username),
                "must_change_password": must_change_password,
                "is_active": True,
                "failed_login_count": 0,
                "locked_until": None,
                "last_login_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        await database["user_project_roles"].insert_one(
            {
                "user_id": result.inserted_id,
                "project_id": project_id,
                "role": role,
                "created_at": now,
                "updated_at": now,
            }
        )
        return result.inserted_id

    return asyncio.run(insert())


def _login(client: TestClient, username: str, password: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()


def test_login_session_csrf_logout_and_security_headers(
    auth_client: tuple[TestClient, Any],
) -> None:
    client, database = auth_client
    _insert_user(database, "admin@example.test", "Existing-Password-123!")

    result = _login(client, "ADMIN@example.test", "Existing-Password-123!")
    assert result["user"]["must_change_password"] is False
    session_cookie = next(cookie for cookie in client.cookies.jar if cookie.name == "defectdna_session")
    assert session_cookie.has_nonstandard_attr("HttpOnly")
    assert session_cookie.secure
    assert client.get("/api/v1/me").status_code == 200

    rejected = client.post("/api/v1/auth/logout")
    assert rejected.status_code == 403
    logged_out = client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": result["csrf_token"]}
    )
    assert logged_out.status_code == 204
    assert client.get("/api/v1/me").status_code == 401

    health = client.get("/health")
    assert health.headers["X-Content-Type-Options"] == "nosniff"
    assert health.headers["X-Frame-Options"] == "DENY"


def test_first_login_password_change_and_admin_user_creation(
    auth_client: tuple[TestClient, Any],
) -> None:
    client, database = auth_client
    _insert_user(database, "admin@example.test", "Existing-Password-123!")
    login = _login(client, "admin@example.test", "Existing-Password-123!")
    headers = {"X-CSRF-Token": login["csrf_token"]}

    created = client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "new.user@example.test", "display_name": "New User"},
    )
    assert created.status_code == 201
    user_id = created.json()["user"]["id"]
    assigned = client.put(
        f"/api/v1/users/{user_id}/roles/project-a",
        headers=headers,
        json={"role": "VIEWER"},
    )
    assert assigned.status_code == 200
    reset = client.post(
        f"/api/v1/users/{user_id}/reset-password",
        headers=headers,
        json={"project_id": "project-a"},
    )
    assert reset.status_code == 200
    assert (
        asyncio.run(database["audit_log"].count_documents({"action": "ROLE_CHANGED"})) == 1
    )
    assert (
        asyncio.run(database["audit_log"].count_documents({"action": "PASSWORD_RESET"})) == 1
    )

    temporary_password = reset.json()["temporary_password"]
    first_login = _login(client, "new.user@example.test", temporary_password)
    forced = client.get("/api/v1/me")
    assert forced.status_code == 403
    assert forced.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"

    changed = client.post(
        "/api/v1/auth/change-password",
        headers={"X-CSRF-Token": first_login["csrf_token"]},
        json={
            "current_password": temporary_password,
            "new_password": "New-Strong-Password-456!",
        },
    )
    assert changed.status_code == 200
    profile = client.get("/api/v1/me")
    assert profile.status_code == 200
    assert profile.json()["user"]["must_change_password"] is False


def test_login_lockout_and_audit_records(
    auth_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, database = auth_client
    _insert_user(database, "admin@example.test", "Existing-Password-123!")
    monkeypatch.setattr(settings, "login_max_failures", 2)

    first = client.post(
        "/api/v1/auth/login",
        json={"username": "admin@example.test", "password": "Wrong-Password-123!"},
    )
    second = client.post(
        "/api/v1/auth/login",
        json={"username": "admin@example.test", "password": "Wrong-Password-123!"},
    )
    correct = client.post(
        "/api/v1/auth/login",
        json={"username": "admin@example.test", "password": "Existing-Password-123!"},
    )

    assert first.status_code == 401
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "ACCOUNT_LOCKED"
    assert correct.status_code == 429
    assert asyncio.run(database["audit_log"].count_documents({"action": "LOGIN_FAILED"})) == 1
    assert asyncio.run(database["audit_log"].count_documents({"action": "LOGIN_LOCKED"})) == 2


def test_viewer_cannot_manage_users_and_admin_cannot_cross_project(
    auth_client: tuple[TestClient, Any],
) -> None:
    client, database = auth_client
    _insert_user(database, "viewer@example.test", "Existing-Password-123!", role="VIEWER")
    viewer_login = _login(client, "viewer@example.test", "Existing-Password-123!")
    denied = client.post(
        "/api/v1/users",
        headers={"X-CSRF-Token": viewer_login["csrf_token"]},
        json={"username": "another.user@example.test", "display_name": "Another User"},
    )
    assert denied.status_code == 403
    qa_database_user = _insert_user(
        database,
        "lead@example.test",
        "Lead-Password-123!",
        role="QA_LEAD",
    )
    assert qa_database_user
    client.cookies.clear()
    lead_login = _login(client, "lead@example.test", "Lead-Password-123!")
    lead_denied = client.post(
        "/api/v1/users",
        headers={"X-CSRF-Token": lead_login["csrf_token"]},
        json={"username": "third.user@example.test", "display_name": "Third User"},
    )
    assert lead_denied.status_code == 403

    client.cookies.clear()
    _insert_user(database, "admin@example.test", "Another-Password-456!", role="ADMIN")
    _insert_user(
        database,
        "second@example.test",
        "Another-Password-456!",
        project_id="project-b",
        role="VIEWER",
    )
    client.cookies.clear()
    admin_login = _login(client, "admin@example.test", "Another-Password-456!")
    target_id = asyncio.run(
        database["users"].find_one({"username": "second@example.test"})
    )["_id"]
    cross_project = client.put(
        f"/api/v1/users/{target_id}/roles/project-b",
        headers={"X-CSRF-Token": admin_login["csrf_token"]},
        json={"role": "ADMIN"},
    )
    assert cross_project.status_code == 403


def test_validation_errors_do_not_echo_passwords(
    auth_client: tuple[TestClient, Any],
) -> None:
    client, _ = auth_client
    password = "Sensitive-Input-123!"
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "invalid username", "password": password},
    )

    assert response.status_code == 422
    assert password not in response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_rate_limit_applies_to_authenticated_user(
    auth_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, database = auth_client
    _insert_user(database, "admin@example.test", "Existing-Password-123!")
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 1)
    _login(client, "admin@example.test", "Existing-Password-123!")

    assert client.get("/api/v1/me").status_code == 200
    limited = client.get("/api/v1/me")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
