from libs.common.taxonomy import starter_taxonomy
from services.ai_knowledge_service.app.analysis import AnalysisOutcome
from services.ai_knowledge_service.app.pipeline.validate import RcaProposal
from services.ai_knowledge_service.evals.build_golden_set import clean_comments
from services.ai_knowledge_service.evals.run_golden_set import score


def _outcome(code: str | None, confidence: float = 0.9) -> AnalysisOutcome:
    proposal = (
        RcaProposal(code, "DEVELOPMENT", "NO_TEST_CASE", None, confidence, "Because.")
        if code
        else None
    )
    return AnalysisOutcome(
        status="AI_PROPOSED" if code else "NEEDS_MANUAL_RCA",
        proposal=proposal,
        failure_reason=None if code else "INVALID_JSON",
        llm_attempts=1,
        model="fake",
    )


def test_review_comment_and_author_names_are_removed() -> None:
    text = (
        "[2026-11-20 Jane Doe] [Support] Ticket raised.\n\n"
        "[2026-11-20 Jane Doe] [Dev] Root cause: wrong binding.\n\n"
        "[2026-11-21 Jane Doe] [RCA review] Coding defect: logic was wrong."
    )
    cleaned = clean_comments(text)
    assert "RCA review" not in cleaned
    assert "Jane" not in cleaned
    assert cleaned.startswith("[2026-11-20] [Support]")


def test_score_counts_matches_manual_items_and_confusion() -> None:
    cases = [
        {"id": f"ADO-{i}", "defect": {"ado_id": i}, "expected": {"root_cause_code": "CODE"}}
        for i in (1, 2, 3)
    ]
    outcomes = {1: _outcome("CODE"), 2: _outcome("DATA", 0.5), 3: _outcome(None)}
    result = score(cases, outcomes, starter_taxonomy())

    assert result["match_rate"] == round(1 / 3, 4)
    assert result["ai_proposed"] == 2
    assert result["needs_manual_rca"] == 1
    assert result["taxonomy_conformance"] == 1.0
    assert result["root_cause_confusion"]["CODE"] == {
        "CODE": 1,
        "DATA": 1,
        "NEEDS_MANUAL_RCA": 1,
    }
    assert {miss["id"] for miss in result["mismatches"]} == {"ADO-2", "ADO-3"}
