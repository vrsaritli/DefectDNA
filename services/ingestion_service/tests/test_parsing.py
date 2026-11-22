from io import BytesIO

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from services.ingestion_service.app.parsing import parse_defect_file


def test_csv_parsing_maps_fields_and_reports_bad_rows() -> None:
    content = (
        "ID,Title,Area Path,Iteration Path,Closed Date,Tags\n"
        "48213,Checkout total,Checkout,R2026.3,2026-09-10,\"regression;billing\"\n"
        "not-an-id,Missing id,Checkout,R2026.3,2026-09-10,\n"
        "48215,,Checkout,R2026.3,not-a-date,\n"
    ).encode()

    result = parse_defect_file("defects.csv", content)

    assert result["rows_total"] == 3
    assert result["rows_valid"] == 1
    assert result["mapped"]["ID"] == "ado_id"
    assert result["mapped"]["Area Path"] == "module"
    assert result["defects"][0]["ado_id"] == 48213
    assert result["defects"][0]["tags"] == ["regression", "billing"]
    assert [error["row"] for error in result["row_errors"]] == [3, 4, 4]


def test_explicit_mapping_and_formula_text_are_kept_as_plain_data() -> None:
    result = parse_defect_file(
        "defects.csv",
        b"Work,Name\n8,=HYPERLINK(\"https://example.test\")\n",
        mapping={"Work": "ado_id", "Name": "title"},
    )

    assert result["defects"][0]["title"] == '=HYPERLINK("https://example.test")'


def test_xlsx_parsing_and_missing_required_columns() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["ID", "Title", "Closed Date"])
    worksheet.append([9, "Password reset", "11/22/2026"])
    buffer = BytesIO()
    workbook.save(buffer)

    parsed = parse_defect_file("defects.xlsx", buffer.getvalue())

    assert parsed["rows_valid"] == 1
    assert parsed["defects"][0]["ado_id"] == 9

    with pytest.raises(HTTPException) as error:
        parse_defect_file("defects.csv", b"Description\nno title\n")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "MISSING_REQUIRED_COLUMNS"


def test_duplicate_ids_are_reported_and_row_limit_is_enforced() -> None:
    content = b"ID,Title\n1,First\n1,Duplicate\n"
    result = parse_defect_file("defects.csv", content)
    assert result["rows_valid"] == 1
    assert result["row_errors"] == [
        {"row": 3, "field": "ado_id", "issue": "duplicate defect ID in file"}
    ]

    with pytest.raises(HTTPException) as error:
        parse_defect_file("defects.csv", content, max_rows=1)
    assert error.value.status_code == 413
    assert error.value.detail["code"] == "TOO_MANY_ROWS"


def test_invalid_xlsx_is_reported_as_validation_error() -> None:
    with pytest.raises(HTTPException) as error:
        parse_defect_file("defects.xlsx", b"not an xlsx archive")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "INVALID_XLSX"
