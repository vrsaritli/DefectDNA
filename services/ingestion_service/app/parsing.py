import csv
import io
import itertools
import re
import zipfile
from datetime import date, datetime
from typing import Any

from fastapi import HTTPException
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

MAX_PREVIEW_ROWS = 10
_CANONICAL_FIELDS = {
    "ado_id",
    "title",
    "description",
    "repro_steps",
    "comments",
    "module",
    "release",
    "severity",
    "priority",
    "state",
    "created_date",
    "closed_date",
    "tags",
    "ado_root_cause",
    "resolved_reason",
    "disposition",
}
_ALIASES = {
    "ado_id": {"id", "ado id", "work item id", "workitem id", "defect id"},
    "title": {"title", "work item title", "defect title"},
    "description": {"description"},
    "repro_steps": {"repro steps", "reproduction steps", "steps to reproduce"},
    "comments": {"comments", "discussion"},
    "module": {"module", "area path", "component"},
    "release": {"release", "iteration path", "iteration", "milestone"},
    "severity": {"severity"},
    "priority": {"priority"},
    "state": {"state", "status"},
    "created_date": {"created date", "created"},
    "closed_date": {"closed date", "resolved date", "closed"},
    "tags": {"tags", "tag"},
    # The root cause QA recorded in ADO; shown beside the AI proposal, never sent to the AI.
    "ado_root_cause": {
        "root cause (rca)",
        "rca root cause",
        "root cause",
        "confirmed root cause",
        "ct_root cause",
        "root_cause",
        "root_cause_raw",
        "rca",
    },
    "resolved_reason": {"resolved reason", "resolution", "resolution reason"},
    "disposition": {"disposition", "ct_disposition"},
}
_DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%d-%b-%Y",
)


def _header_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _detect_mapping(headers: list[str], mapping: dict[str, str] | None) -> dict[str, str]:
    if mapping is not None:
        mapped: dict[str, str] = {}
        headers_by_key = {_header_key(header): header for header in headers}
        for source, field in mapping.items():
            actual_source = headers_by_key.get(_header_key(source))
            if actual_source is None or field not in _CANONICAL_FIELDS:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "INVALID_COLUMN_MAPPING"},
                )
            if actual_source in mapped or field in mapped.values():
                raise HTTPException(
                    status_code=422,
                    detail={"code": "DUPLICATE_COLUMN_MAPPING"},
                )
            mapped[actual_source] = field
    else:
        mapped = {}
        for header in headers:
            key = _header_key(header)
            for field, aliases in _ALIASES.items():
                if key == field or key in aliases:
                    mapped[header] = field
                    break

    if not {"ado_id", "title"}.issubset(mapped.values()):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MISSING_REQUIRED_COLUMNS",
                "message": "Columns for ado_id and title are required",
            },
        )
    return mapped


def _parse_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for date_format in _DATE_FORMATS:
            try:
                return datetime.strptime(text, date_format)
            except ValueError:
                continue
    raise ValueError("unparseable date")


def _read_rows(
    filename: str,
    content: bytes,
    max_rows: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    suffix = filename.rpartition(".")[2].casefold()
    if suffix == "csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_FILE_ENCODING"},
            ) from exc
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise HTTPException(status_code=422, detail={"code": "EMPTY_FILE"})
        return [str(header) for header in reader.fieldnames], list(
            itertools.islice(reader, max_rows + 1)
        )
    if suffix == "xlsx":
        workbook = None
        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            worksheet = workbook.active
            if worksheet is None:
                raise HTTPException(status_code=422, detail={"code": "EMPTY_FILE"})
            values = worksheet.iter_rows(values_only=True)
            header_row = next(values, None)
            if header_row is None:
                raise HTTPException(status_code=422, detail={"code": "EMPTY_FILE"})
            headers = [str(value or "").strip() for value in header_row]
            rows = []
            for row in values:
                if any(value is not None for value in row):
                    rows.append(dict(zip(headers, row, strict=False)))
                    if len(rows) > max_rows:
                        break
            return headers, rows
        except HTTPException:
            raise
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, InvalidFileException) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_XLSX"},
            ) from exc
        finally:
            if workbook is not None:
                workbook.close()
    raise HTTPException(status_code=415, detail={"code": "UNSUPPORTED_FILE_TYPE"})


def parse_defect_file(
    filename: str,
    content: bytes,
    *,
    mapping: dict[str, str] | None = None,
    max_rows: int = 5_000,
) -> dict[str, Any]:
    headers, rows = _read_rows(filename, content, max_rows)
    if len(rows) > max_rows:
        raise HTTPException(status_code=413, detail={"code": "TOO_MANY_ROWS"})
    column_mapping = _detect_mapping(headers, mapping)
    accepted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for row_number, row in enumerate(rows, start=2):
        defect: dict[str, Any] = {}
        row_errors: list[dict[str, str]] = []
        for source, field in column_mapping.items():
            value = row.get(source)
            if field == "ado_id":
                try:
                    defect[field] = int(str(value).strip())
                    if defect[field] < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    row_errors.append({"field": source, "issue": "must be a positive integer"})
            elif field in {"created_date", "closed_date"}:
                try:
                    defect[field] = _parse_date(value)
                except ValueError as exc:
                    row_errors.append({"field": source, "issue": str(exc)})
            elif field == "tags":
                if isinstance(value, (list, tuple)):
                    defect[field] = [str(tag).strip() for tag in value if str(tag).strip()]
                else:
                    defect[field] = [
                        tag.strip()
                        for tag in re.split(r"[;,]", str(value or ""))
                        if tag.strip()
                    ]
            elif value is not None:
                defect[field] = str(value).strip()

        if not defect.get("title"):
            row_errors.append({"field": "title", "issue": "required"})
        if row_errors:
            errors.extend(
                {"row": row_number, "field": error["field"], "issue": error["issue"]}
                for error in row_errors
            )
            continue
        if defect["ado_id"] in seen_ids:
            errors.append(
                {
                    "row": row_number,
                    "field": "ado_id",
                    "issue": "duplicate defect ID in file",
                }
            )
            continue
        seen_ids.add(defect["ado_id"])

        for optional in (
            "description",
            "repro_steps",
            "comments",
            "module",
            "release",
            "severity",
            "priority",
            "state",
        ):
            defect.setdefault(optional, "")
        defect["source"] = "CSV_IMPORT"
        accepted.append(defect)

    return {
        "detected_columns": headers,
        "mapped": column_mapping,
        "rows_total": len(rows),
        "rows_valid": len(accepted),
        "row_errors": errors,
        "preview_rows": accepted[:MAX_PREVIEW_ROWS],
        "defects": accepted,
    }


# Columns that hold a QA-confirmed RCA in seed files (historical, reviewed defects).
# "SDLC Phase" alone is deliberately absent: in ADO exports it usually means where the
# defect was found, not where it was introduced.
_CONFIRMED_ALIASES = {
    "root_cause": {
        "root cause (rca)",
        "rca root cause",
        "root cause",
        "confirmed root cause",
        "ct_root cause",
        "root_cause",
        "rca",
    },
    "sdlc_phase": {
        "sdlc phase (rca)",
        "rca sdlc phase",
        "sdlc phase introduced",
        "phase introduced",
        "introduced in phase",
        "confirmed sdlc phase",
    },
    "testing_gap": {
        "testing gap (rca)",
        "rca testing gap",
        "testing gap",
        "confirmed testing gap",
    },
}


def extract_confirmed_labels(
    filename: str, content: bytes, *, max_rows: int = 5_000
) -> tuple[dict[str, str], dict[int, dict[str, str]]]:
    """Map defect ID -> confirmed RCA labels found in seed columns.

    Returns (detected label columns, labels by ado_id). Rows without a root cause are
    skipped; values are returned as text for the gateway to map onto taxonomy codes.
    """
    headers, rows = _read_rows(filename, content, max_rows)
    by_key = {_header_key(header): header for header in headers}
    id_header = next(
        (by_key[key] for key in by_key if key == "ado_id" or key in _ALIASES["ado_id"]), None
    )
    columns: dict[str, str] = {}
    for field, aliases in _CONFIRMED_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if alias in by_key:
                columns[field] = by_key[alias]
                break
    labels: dict[int, dict[str, str]] = {}
    if id_header is None or "root_cause" not in columns:
        return columns, labels
    for row in rows[:max_rows]:
        try:
            ado_id = int(str(row.get(id_header)).strip())
        except (TypeError, ValueError):
            continue
        values = {
            field: str(row.get(header) or "").strip() for field, header in columns.items()
        }
        if values.get("root_cause"):
            labels[ado_id] = {key: value for key, value in values.items() if value}
    return columns, labels