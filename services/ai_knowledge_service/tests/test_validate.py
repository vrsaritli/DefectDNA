import json
from typing import Any

import pytest

from libs.common.taxonomy import starter_taxonomy
from services.ai_knowledge_service.app.pipeline.validate import (
    ProposalValidationError,
    validate_batch,
    validate_item,
)

TAXONOMY = starter_taxonomy()


def _item(**overrides: Any) -> dict[str, Any]:
    item = {
        "root_cause_code": "CODE",
        "sdlc_phase_code": "DEVELOPMENT",
        "testing_gap_code": "NO_TEST_CASE",
        "other_proposal": "",
        "confidence": 0.7,
        "rationale": "Evidence in the dev comment.",
    }
    item.update(overrides)
    return item


def test_valid_item_is_accepted() -> None:
    proposal = validate_item(_item(), TAXONOMY, max_rationale_chars=600)
    assert proposal.root_cause_code == "CODE"
    assert proposal.other_proposal is None


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"root_cause_code": "MADE_UP"}, "UNKNOWN_ROOT_CAUSE_CODE"),
        ({"sdlc_phase_code": "code"}, "UNKNOWN_SDLC_PHASE_CODE"),
        ({"testing_gap_code": None}, "UNKNOWN_TESTING_GAP_CODE"),
        ({"confidence": 1.5}, "INVALID_CONFIDENCE"),
        ({"confidence": True}, "INVALID_CONFIDENCE"),
        ({"confidence": float("nan")}, "INVALID_CONFIDENCE"),
        ({"rationale": "  "}, "EMPTY_RATIONALE"),
        ({"root_cause_code": "OTHER", "other_proposal": ""}, "OTHER_WITHOUT_PROPOSAL"),
        ({"extra": "field"}, "UNEXPECTED_FIELDS"),
    ],
)
def test_invalid_items_are_rejected(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(ProposalValidationError) as error:
        validate_item(_item(**overrides), TAXONOMY, max_rationale_chars=600)
    assert error.value.code == code


def test_other_proposal_kept_only_when_other_used() -> None:
    proposal = validate_item(
        _item(root_cause_code="OTHER", other_proposal="Vendor licence expiry"),
        TAXONOMY,
        max_rationale_chars=600,
    )
    assert proposal.other_proposal == "Vendor licence expiry"
    assert validate_item(
        _item(other_proposal="ignored"), TAXONOMY, max_rationale_chars=600
    ).other_proposal is None


def test_long_rationale_is_truncated() -> None:
    proposal = validate_item(_item(rationale="x" * 1000), TAXONOMY, max_rationale_chars=100)
    assert len(proposal.rationale) == 100


def test_batch_accounts_for_every_ref() -> None:
    raw = json.dumps(
        {
            "results": [
                {"ref": "ADO-1", **_item()},
                {"ref": "ADO-2", **_item(root_cause_code="NOPE")},
                {"ref": "ADO-3", **_item()},
                {"ref": "ADO-3", **_item()},
                {"ref": "ADO-99", **_item()},
            ]
        }
    )
    result = validate_batch(
        raw, TAXONOMY, ["ADO-1", "ADO-2", "ADO-3", "ADO-4"], max_rationale_chars=600
    )
    assert set(result.proposals) == {"ADO-1"}
    assert result.errors == {
        "ADO-2": "UNKNOWN_ROOT_CAUSE_CODE",
        "ADO-3": "DUPLICATE_RESULT",
        "ADO-4": "MISSING_RESULT",
    }


def test_batch_with_invalid_json_fails_every_ref() -> None:
    result = validate_batch("not json", TAXONOMY, ["ADO-1", "ADO-2"], max_rationale_chars=600)
    assert result.proposals == {}
    assert result.errors == {"ADO-1": "INVALID_JSON", "ADO-2": "INVALID_JSON"}
