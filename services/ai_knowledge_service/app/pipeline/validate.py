"""Validate LLM output against the taxonomy version used for the request.

Anything that does not conform is rejected; nothing outside the schema is stored.
"""

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from libs.common.taxonomy import OTHER_CODE, Taxonomy, TaxonomyKind

_FIELDS: dict[str, TaxonomyKind] = {
    "root_cause_code": "root_causes",
    "sdlc_phase_code": "sdlc_phases",
    "testing_gap_code": "testing_gaps",
}
_OPTIONAL_KEYS = frozenset({"other_proposal", "evidence_fields"})
_ITEM_KEYS = frozenset({*_FIELDS, *_OPTIONAL_KEYS, "confidence", "rationale"})
# Record sections a proposal may name as its evidence (rca-v3 ``evidence_fields``).
EVIDENCE_FIELDS = frozenset(
    {
        "title",
        "description",
        "repro_steps",
        "comments",
        "tags",
        "module",
        "release",
        "severity",
        "priority",
        "similar_cases",
    }
)
MAX_OTHER_PROPOSAL_CHARS = 80


class ProposalValidationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RcaProposal:
    root_cause_code: str
    sdlc_phase_code: str
    testing_gap_code: str
    other_proposal: str | None
    confidence: float
    rationale: str
    evidence_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchValidation:
    proposals: dict[str, RcaProposal] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    citations: dict[str, list[str]] = field(default_factory=dict)


def _validate_citations(value: Any, retrieved: int) -> list[str]:
    """Cited case refs must be a subset of the refs retrieved for that record."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProposalValidationError("INVALID_CITATIONS")
    allowed = {f"C{index}" for index in range(1, retrieved + 1)}
    if any(item not in allowed for item in value):
        raise ProposalValidationError("CITATION_NOT_RETRIEVED")
    return list(dict.fromkeys(value))


def validate_item(
    data: Any, taxonomy: Taxonomy, *, max_rationale_chars: int
) -> RcaProposal:
    if not isinstance(data, dict):
        raise ProposalValidationError("NOT_AN_OBJECT")
    keys = set(data)
    if keys - _ITEM_KEYS:
        raise ProposalValidationError("UNEXPECTED_FIELDS")
    if _ITEM_KEYS - keys - _OPTIONAL_KEYS:
        raise ProposalValidationError("MISSING_FIELDS")

    codes: dict[str, str] = {}
    for name, kind in _FIELDS.items():
        value = data[name]
        if not isinstance(value, str) or value not in taxonomy.codes(kind):
            raise ProposalValidationError(f"UNKNOWN_{name.upper()}")
        codes[name] = value

    confidence = data["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ProposalValidationError("INVALID_CONFIDENCE")

    rationale = data["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise ProposalValidationError("EMPTY_RATIONALE")
    rationale = rationale.strip()
    if len(rationale) > max_rationale_chars:
        rationale = rationale[: max_rationale_chars - 1].rstrip() + "…"

    other = data.get("other_proposal", "")
    if other is not None and not isinstance(other, str):
        raise ProposalValidationError("INVALID_OTHER_PROPOSAL")
    other = (other or "").strip()
    uses_other = OTHER_CODE in codes.values()
    if uses_other and not other:
        raise ProposalValidationError("OTHER_WITHOUT_PROPOSAL")

    evidence = data.get("evidence_fields") or []
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ProposalValidationError("INVALID_EVIDENCE_FIELDS")
    evidence = [item for item in dict.fromkeys(evidence) if item in EVIDENCE_FIELDS]

    return RcaProposal(
        root_cause_code=codes["root_cause_code"],
        sdlc_phase_code=codes["sdlc_phase_code"],
        testing_gap_code=codes["testing_gap_code"],
        other_proposal=other[:MAX_OTHER_PROPOSAL_CHARS] if uses_other else None,
        confidence=round(float(confidence), 4),
        rationale=rationale,
        evidence_fields=evidence,
    )


def validate_batch(
    raw: str,
    taxonomy: Taxonomy,
    refs: list[str],
    *,
    max_rationale_chars: int,
    case_counts: dict[str, int] | None = None,
) -> BatchValidation:
    """Validate a batch answer. Every requested ref ends up in proposals or errors."""
    result = BatchValidation()

    def fail_all(code: str) -> BatchValidation:
        result.errors = {ref: code for ref in refs}
        return result

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return fail_all("INVALID_JSON")
    if not isinstance(data, dict) or set(data) != {"results"}:
        return fail_all("UNEXPECTED_FIELDS")
    items = data["results"]
    if not isinstance(items, list):
        return fail_all("NOT_A_LIST")

    seen: dict[str, int] = {}
    for item in items:
        ref = item.get("ref") if isinstance(item, dict) else None
        if isinstance(ref, str):
            seen[ref] = seen.get(ref, 0) + 1

    wanted = set(refs)
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        if ref not in wanted or seen.get(ref, 0) != 1:
            continue
        body = {key: value for key, value in item.items() if key not in {"ref", "cited_cases"}}
        try:
            citations = _validate_citations(
                item.get("cited_cases"), (case_counts or {}).get(ref, 0)
            )
            result.proposals[ref] = validate_item(
                body, taxonomy, max_rationale_chars=max_rationale_chars
            )
            result.citations[ref] = citations
        except ProposalValidationError as exc:
            result.errors[ref] = exc.code

    for ref in refs:
        if ref in result.proposals or ref in result.errors:
            continue
        result.errors[ref] = "DUPLICATE_RESULT" if seen.get(ref, 0) > 1 else "MISSING_RESULT"
    return result
