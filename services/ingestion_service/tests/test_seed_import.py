import io
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from openpyxl import Workbook

from libs.common.config import settings
from libs.common.service_auth import create_service_token
from services.ingestion_service.app.main import app
from services.ingestion_service.app.parsing import extract_confirmed_labels


def _xlsx(rows: list[list[Any]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


SEED = _xlsx(
    [
        ["ID", "Title", "State", "SDLC Phase", "Root Cause (RCA)", "Area Path"],
        [301, "Surcharge missing", "Closed", "In UAT", "Code", "Shop\\Payments"],
        [302, "Search slow", "Closed", "Production", None, "Shop\\Search"],
        [303, "Stale results", "Closed", "Production", "Test Gap", "Shop\\Search"],
    ]
)


@pytest.fixture
def ingestion(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, str]]]:
    mongo = AsyncMongoMockClient(tz_aware=True)
    monkeypatch.setattr(settings, "service_token_secret", "ingestion-seed-secret-" * 2)
    headers = {
        "Authorization": f"Bearer {create_service_token(settings.service_token_secret, 'api-gateway')}",
        "X-Project-ID": "project-a",
        "X-User-ID": "65a000000000000000000001",
    }
    with TestClient(app) as client:
        app.state.mongo_client = mongo
        yield client, headers
    app.state.mongo_client = None
    mongo.close()


def test_found_in_sdlc_column_is_not_treated_as_confirmed_phase() -> None:
    columns, labels = extract_confirmed_labels("seed.xlsx", SEED)
    assert columns == {"root_cause": "Root Cause (RCA)"}
    assert labels == {301: {"root_cause": "Code"}, 303: {"root_cause": "Test Gap"}}


def test_seed_import_stores_defects_and_returns_labels(
    ingestion: tuple[TestClient, dict[str, str]],
) -> None:
    client, headers = ingestion
    response = client.post(
        "/internal/seed-imports",
        headers=headers,
        files={"file": ("seed.xlsx", SEED, "application/octet-stream")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["preview"]["rows_valid"] == 3
    confirmed = {item["defect"]["ado_id"]: item["labels"] for item in body["confirmed"]}
    assert confirmed == {301: {"root_cause": "Code"}, 303: {"root_cause": "Test Gap"}}

    no_rca = client.post(
        "/internal/seed-imports",
        headers=headers,
        files={"file": ("plain.csv", b"ID,Title\n1,Defect\n", "text/csv")},
    )
    assert no_rca.status_code == 422
    assert no_rca.json()["detail"]["code"] == "MISSING_RCA_COLUMNS"
