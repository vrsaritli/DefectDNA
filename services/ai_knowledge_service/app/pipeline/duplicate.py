"""Rule-based duplicate detection, applied before any AI call.

A duplicate has no root cause of its own: the original defect carries it. A defect is a
duplicate when its ADO resolved reason or disposition says so, or when a comment or the
description says "duplicate of #123" (not "not a duplicate of ...").
"""

import re
from typing import Any

_DUPLICATE_OF = re.compile(
    r"(?<!not )(?<!not a )\bduplicate\s+(?:of|to)\s*:?\s*(?:bug|defect|work item|item|ado)?"
    r"\s*[-#]?\s*(\d{1,9})\b",
    re.IGNORECASE,
)


def _comments_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value
        )
    return str(value or "")


def detect_duplicate(defect: dict[str, Any]) -> tuple[bool, int | None]:
    """(is_duplicate, ADO ID of the original defect when one is referenced)."""
    own_id = defect.get("ado_id")
    original: int | None = None
    for text in (_comments_text(defect.get("comments")), str(defect.get("description") or "")):
        for match in _DUPLICATE_OF.finditer(text):
            candidate = int(match.group(1))
            if candidate != own_id:
                original = candidate
                break
        if original is not None:
            break
    marked = any(
        "duplicate" in str(defect.get(name) or "").casefold()
        for name in ("resolved_reason", "disposition")
    )
    return (marked or original is not None), original
