import json
from collections.abc import Callable
from typing import Any

import pytest

from services.ai_knowledge_service.app.adapters.llm import LLMResponse


def make_answer(refs: list[str], **overrides: Any) -> LLMResponse:
    """A schema-valid batch answer for the given refs; overrides apply to every item."""
    items = []
    for ref in refs:
        item = {
            "ref": ref,
            "root_cause_code": "CODE",
            "sdlc_phase_code": "DEVELOPMENT",
            "testing_gap_code": "INSUFFICIENT_COVERAGE",
            "other_proposal": "",
            "confidence": 0.8,
            "rationale": "The developer comment names a wrong parameter binding.",
        }
        item.update(overrides)
        items.append(item)
    return LLMResponse(
        content=json.dumps({"results": items}),
        model="fake-model",
        usage={"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
    )


class FakeLLM:
    model = "fake-model"

    def __init__(self, responder: Callable[[list[str], int], LLMResponse]) -> None:
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        schema_name: str,
        max_completion_tokens: int | None = None,
    ) -> LLMResponse:
        refs = schema["properties"]["results"]["items"]["properties"]["ref"]["enum"]
        self.calls.append(
            {
                "refs": list(refs),
                "messages": messages,
                "max_completion_tokens": max_completion_tokens,
            }
        )
        return self.responder(list(refs), len(self.calls))

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_llm() -> Callable[[Callable[[list[str], int], LLMResponse]], FakeLLM]:
    return FakeLLM


@pytest.fixture
def answer() -> Callable[..., LLMResponse]:
    return make_answer


def sample_defect(ado_id: int, **overrides: Any) -> dict[str, Any]:
    defect = {
        "ado_id": ado_id,
        "title": f"Saved card cannot be deleted ({ado_id})",
        "description": "Reported by customer support.",
        "repro_steps": "Open saved cards\nDelete a card\nExpected: removed\nActual: still listed",
        "comments": "[Dev] Root cause: the delete endpoint ignored the token id parameter.",
        "module": "Shop\\API\\Payments",
        "release": "Shop\\Release 1\\Sprint 2",
        "severity": "2 - High",
        "priority": "2",
        "state": "Closed",
        "tags": ["payments"],
    }
    defect.update(overrides)
    return defect


@pytest.fixture
def defect_factory() -> Callable[..., dict[str, Any]]:
    return sample_defect
