from services.ai_knowledge_service.app.pipeline.duplicate import detect_duplicate


def test_duplicate_reference_in_comment_is_found() -> None:
    defect = {
        "ado_id": 303,
        "comments": "[2026-11-20 Dev] [Dev] Duplicate of #608; closing this one.",
    }
    assert detect_duplicate(defect) == (True, 608)


def test_resolved_reason_marks_a_duplicate_without_reference() -> None:
    assert detect_duplicate({"ado_id": 1, "resolved_reason": "Duplicate"}) == (True, None)
    assert detect_duplicate({"ado_id": 1, "disposition": "Duplicate"}) == (True, None)


def test_comment_list_and_other_phrasings() -> None:
    defect = {"ado_id": 5, "comments": [{"text": "This is a duplicate of bug 4410."}]}
    assert detect_duplicate(defect) == (True, 4410)


def test_non_duplicates_are_not_flagged() -> None:
    assert detect_duplicate({"ado_id": 7, "comments": "Not a duplicate of #608."}) == (False, None)
    assert detect_duplicate({"ado_id": 7, "resolved_reason": "Fixed"}) == (False, None)
    assert detect_duplicate({"ado_id": 7, "comments": "Duplicate of #7"}) == (False, None)
