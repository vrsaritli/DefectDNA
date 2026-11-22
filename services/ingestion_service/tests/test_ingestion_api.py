import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from libs.common.config import settings
from libs.common.service_auth import create_service_token
from services.ingestion_service.app.main import app


@pytest.fixture
def ingestion_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Any, dict[str, str]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    monkeypatch.setattr(settings, "service_token_secret", "ingestion-test-secret-" * 2)
    monkeypatch.setattr(settings, "max_import_file_bytes", 1024)
    headers = {
        "Authorization": (
            f"Bearer {create_service_token(settings.service_token_secret, 'api-gateway')}"
        ),
        "X-Project-ID": "project-a",
        "X-User-ID": "65a000000000000000000001",
    }
    with TestClient(app) as client:
        app.state.mongo_client = mongo
        yield client, mongo, headers
    app.state.mongo_client = None
    mongo.close()


def test_internal_import_is_authorized_and_idempotent(
    ingestion_client: tuple[TestClient, Any, dict[str, str]],
) -> None:
    client, mongo, headers = ingestion_client

    first = client.post(
        "/internal/imports",
        headers=headers,
        files={
            "file": (
                "defects.csv",
                b"ID,Title,Area Path\n48213,First title,Checkout\nbad,Rejected,Checkout\n",
                "text/csv",
            )
        },
    )
    second = client.post(
        "/internal/imports",
        headers=headers,
        files={"file": ("defects.csv", b"ID,Title\n48213,Updated title\n", "text/csv")},
    )

    assert first.status_code == 200
    assert first.json()["job_id"]
    assert first.json()["preview"]["rows_total"] == 2
    assert first.json()["preview"]["rows_valid"] == 1
    assert first.json()["preview"]["row_errors"][0]["row"] == 3
    assert second.status_code == 200
    assert (
        asyncio.run(
            mongo["defectdna"]["defects"].count_documents({"project_id": "project-a"})
        )
        == 1
    )
    defect = asyncio.run(
        mongo["defectdna"]["defects"].find_one({"project_id": "project-a"})
    )
    assert defect["title"] == "Updated title"
    listed = client.get(
        "/internal/projects/project-a/defects",
        headers=headers,
    )
    detail = client.get(
        f"/internal/projects/project-a/defects/{defect['ado_id']}",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert detail.json()["defect"]["title"] == "Updated title"


def test_internal_import_rejects_bad_token_and_oversized_file(
    ingestion_client: tuple[TestClient, Any, dict[str, str]],
) -> None:
    client, _, headers = ingestion_client
    bad_token = {**headers, "Authorization": "Bearer invalid"}
    rejected = client.post(
        "/internal/imports",
        headers=bad_token,
        files={"file": ("defects.csv", b"ID,Title\n1,test\n", "text/csv")},
    )
    assert rejected.status_code == 401

    too_large = client.post(
        "/internal/imports",
        headers=headers,
        files={"file": ("defects.csv", b"x" * 1025, "text/csv")},
    )
    assert too_large.status_code == 413


def test_seventy_row_import_keeps_bad_rows_non_fatal(
    ingestion_client: tuple[TestClient, Any, dict[str, str]],
) -> None:
    client, mongo, headers = ingestion_client
    rows = ["ID,Title"]
    rows.extend(
        [
            "invalid,Rejected ID" if row_number == 14 else
            f"{row_number}," if row_number == 37 else
            f"{row_number},Defect {row_number}"
            for row_number in range(1, 71)
        ]
    )

    response = client.post(
        "/internal/imports",
        headers=headers,
        files={"file": ("defects.csv", "\n".join(rows).encode(), "text/csv")},
    )

    assert response.status_code == 200
    assert response.json()["preview"]["rows_total"] == 70
    assert response.json()["preview"]["rows_valid"] == 68
    assert len(response.json()["preview"]["row_errors"]) == 2
    assert (
        asyncio.run(
            mongo["defectdna"]["defects"].count_documents({"project_id": "project-a"})
        )
        == 68
    )
