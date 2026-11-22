"""Build the RCA golden set from ADO Excel exports that carry a confirmed root cause.

Selection rule: a defect is a golden case only when the QA "Root Cause (RCA)" column
and the "[RCA review]" comment name the same category. Rows where they disagree are
excluded and listed, because a golden label must be unambiguous.

The "[RCA review]" comment is removed from the model input (it would leak the answer),
and comment author names are dropped.

Usage:
  python -m services.ai_knowledge_service.evals.build_golden_set \
      "<path>/SAmple_demo_Prod_defects.xlsx" "<path>/SAmple_demo_UAT_defects.xlsx" \
      --out services/ai_knowledge_service/evals/golden_set.json
"""

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from libs.common.taxonomy import starter_taxonomy
from services.ingestion_service.app.parsing import parse_defect_file

RCA_COLUMN = "Root Cause (RCA)"
# QA "Root Cause (RCA)" values -> starter taxonomy root-cause codes.
LABEL_TO_CODE = {
    "code": "CODE",
    "configuration": "CONFIGURATION",
    "data": "DATA",
    "integration": "INTEGRATION",
    "requirements": "REQUIREMENTS",
    "test gap": "TEST_GAP",
    "third party": "THIRD_PARTY",
    "security": "SECURITY",
    "design": "DESIGN",
    "deployment": "DEPLOYMENT",
}
# "[RCA review] <kind>: ..." comment kinds -> codes.
REVIEW_TO_CODE = {
    "coding defect": "CODE",
    "environment issue": "CONFIGURATION",
    "data issue": "DATA",
    "integration issue": "INTEGRATION",
    "requirement gap": "REQUIREMENTS",
    "test coverage gap": "TEST_GAP",
    "third-party issue": "THIRD_PARTY",
    "security issue": "SECURITY",
}
_REVIEW = re.compile(r"\[RCA review\]\s*([^:.\n]+)")
_AUTHOR_HEADER = re.compile(r"\[(\d{4}-\d{2}-\d{2})[^\]]*\]")


def _labels(path: Path) -> dict[int, tuple[str | None, str | None]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        header = [str(value or "").strip() for value in next(rows)]
        labels: dict[int, tuple[str | None, str | None]] = {}
        for row in rows:
            data = dict(zip(header, row, strict=False))
            if data.get("ID") is None:
                continue
            review = _REVIEW.search(str(data.get("Comments") or ""))
            label = data.get(RCA_COLUMN)
            labels[int(str(data["ID"]))] = (
                str(label).strip() if label else None,
                review.group(1).strip() if review else None,
            )
        return labels
    finally:
        workbook.close()


def clean_comments(text: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    kept = [part for part in paragraphs if "[RCA review]" not in part]
    return "\n\n".join(_AUTHOR_HEADER.sub(r"[\1]", part) for part in kept)


def _json_safe(defect: dict[str, Any]) -> dict[str, Any]:
    keep = (
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
        "tags",
        "closed_date",
    )
    result: dict[str, Any] = {}
    for key in keep:
        value = defect.get(key)
        result[key] = value.isoformat() if isinstance(value, datetime) else value
    result["comments"] = clean_comments(str(result.get("comments") or ""))
    return result


def build(paths: list[Path]) -> dict[str, Any]:
    taxonomy_codes = set(starter_taxonomy().codes("root_causes"))
    cases: list[dict[str, Any]] = []
    conflicting: list[dict[str, Any]] = []
    unlabelled: list[int] = []
    for path in paths:
        source = path.stem
        parsed = parse_defect_file(path.name, path.read_bytes(), max_rows=100_000)
        labels = _labels(path)
        for defect in parsed["defects"]:
            label, review = labels.get(defect["ado_id"], (None, None))
            label_code = LABEL_TO_CODE.get((label or "").casefold())
            review_code = REVIEW_TO_CODE.get((review or "").casefold())
            if label_code is None or review_code is None:
                unlabelled.append(defect["ado_id"])
                continue
            if label_code != review_code:
                conflicting.append(
                    {
                        "ado_id": defect["ado_id"],
                        "source": source,
                        "rca_column": label,
                        "rca_review": review,
                    }
                )
                continue
            if label_code not in taxonomy_codes:
                raise ValueError(f"{label_code} is not in the starter taxonomy")
            cases.append(
                {
                    "id": f"ADO-{defect['ado_id']}",
                    "source": source,
                    "defect": _json_safe(defect),
                    "expected": {"root_cause_code": label_code},
                    "provenance": {"rca_column": label, "rca_review": review},
                }
            )
    return {
        "name": "defectdna-golden-set",
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_files": [path.name for path in paths],
        "selection_rule": (
            "QA 'Root Cause (RCA)' column and '[RCA review]' comment agree; the review "
            "comment is removed from model input; author names removed."
        ),
        "scored_fields": ["root_cause_code"],
        "cases": cases,
        "excluded": {
            "conflicting_labels": conflicting,
            "unlabelled_or_unmapped_count": len(unlabelled),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    golden = build(args.inputs)
    args.out.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"{len(golden['cases'])} golden cases, "
        f"{len(golden['excluded']['conflicting_labels'])} excluded for conflicting labels, "
        f"{golden['excluded']['unlabelled_or_unmapped_count']} unlabelled -> {args.out}"
    )


if __name__ == "__main__":
    main()
