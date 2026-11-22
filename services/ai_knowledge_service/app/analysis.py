"""RCA pipeline: scrub -> pack into token-budgeted batches -> LLM -> validate -> one retry.

Token budget per Groq request (settings, defaults in brackets):
- batches are packed while estimated prompt + expected output <= batch target [4,000];
- ``max_completion_tokens`` = request budget [4,500] - estimated prompt, so the request can
  never exceed the budget; the gap between target and budget is reasoning/output headroom.
"""

import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from libs.common.config import Settings
from libs.common.taxonomy import Taxonomy
from services.ai_knowledge_service.app.adapters.llm import (
    LLMClient,
    LLMOutputError,
    LLMProviderError,
)
from services.ai_knowledge_service.app.pipeline.validate import RcaProposal, validate_batch
from services.ai_knowledge_service.app.prompts.rca_v1 import (
    CHARS_PER_TOKEN,
    MAX_RATIONALE_CHARS,
    PROMPT_VERSION,
    SCHEMA_NAME,
    build_messages,
    build_output_schema,
    build_system_prompt,
    compose_defect_record,
    defect_ref,
    estimate_prompt_tokens,
    render_record,
)

AI_PROPOSED = "AI_PROPOSED"
NEEDS_MANUAL_RCA = "NEEDS_MANUAL_RCA"
MAX_OUTPUT_ATTEMPTS = 2  # first try + one retry (architecture §15 step 6)
_RECORD_JSON_OVERHEAD_CHARS = 160
_MIN_DEFECT_TOKENS = 150

AcquireCall = Callable[[], Awaitable[bool]]


class DailyCapReached(Exception):
    """The project's daily LLM call cap is exhausted; remaining defects stay unanalyzed."""


class BudgetTooSmall(Exception):
    """The fixed prompt (rules + taxonomy) does not fit in the request token budget."""


@dataclass(frozen=True)
class TokenBudget:
    request_budget: int = 4_500
    batch_target: int = 4_000
    output_per_defect: int = 400
    max_defects_per_batch: int = 8

    @classmethod
    def from_settings(cls, settings: Settings) -> "TokenBudget":
        return cls(
            request_budget=settings.llm_request_token_budget,
            batch_target=min(settings.llm_batch_target_tokens, settings.llm_request_token_budget),
            output_per_defect=settings.llm_output_tokens_per_defect,
            max_defects_per_batch=settings.llm_max_defects_per_batch,
        )


@dataclass
class PreparedDefect:
    ado_id: int
    ref: str
    rendered: str
    scrub_counts: dict[str, int]


@dataclass
class Batch:
    items: list[PreparedDefect]
    prompt_tokens_estimate: int
    max_completion_tokens: int


@dataclass
class AnalysisOutcome:
    status: Literal["AI_PROPOSED", "NEEDS_MANUAL_RCA"]
    proposal: RcaProposal | None
    failure_reason: str | None
    llm_attempts: int
    model: str
    prompt_version: str = PROMPT_VERSION
    scrub_counts: dict[str, int] = field(default_factory=dict)
    batch_size: int = 1
    prompt_tokens_estimate: int = 0
    max_completion_tokens: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


@dataclass
class _Attempt:
    proposals: dict[str, RcaProposal]
    errors: dict[str, str]
    retryable: bool
    model: str
    usage: dict[str, int]
    latency_ms: int


def prepare_defects(
    defects: list[dict[str, Any]], taxonomy: Taxonomy, budget: TokenBudget
) -> tuple[str, list[PreparedDefect]]:
    system_prompt = build_system_prompt(taxonomy)
    base = estimate_prompt_tokens(system_prompt, [])
    if base + budget.output_per_defect + _MIN_DEFECT_TOKENS > budget.request_budget:
        raise BudgetTooSmall()
    per_defect_tokens = max(
        _MIN_DEFECT_TOKENS, budget.batch_target - base - budget.output_per_defect
    )
    max_chars = max(600, int(per_defect_tokens * CHARS_PER_TOKEN) - _RECORD_JSON_OVERHEAD_CHARS)
    prepared = []
    for defect in defects:
        ado_id = int(defect["ado_id"])
        record, counts = compose_defect_record(defect, max_chars)
        ref = defect_ref(ado_id)
        prepared.append(
            PreparedDefect(
                ado_id=ado_id,
                ref=ref,
                rendered=render_record(ref, record),
                scrub_counts=counts,
            )
        )
    return system_prompt, prepared


def _make_batch(system_prompt: str, items: list[PreparedDefect], budget: TokenBudget) -> Batch:
    prompt_estimate = estimate_prompt_tokens(system_prompt, [item.rendered for item in items])
    return Batch(
        items=items,
        prompt_tokens_estimate=prompt_estimate,
        max_completion_tokens=max(
            budget.output_per_defect, budget.request_budget - prompt_estimate
        ),
    )


def plan_batches(
    system_prompt: str,
    prepared: list[PreparedDefect],
    budget: TokenBudget,
    *,
    max_per_batch: int | None = None,
) -> list[Batch]:
    limit = max(1, min(max_per_batch or budget.max_defects_per_batch, budget.max_defects_per_batch))
    batches: list[Batch] = []
    current: list[PreparedDefect] = []
    for item in prepared:
        candidate = [*current, item]
        expected = (
            estimate_prompt_tokens(system_prompt, [entry.rendered for entry in candidate])
            + len(candidate) * budget.output_per_defect
        )
        if current and (expected > budget.batch_target or len(candidate) > limit):
            batches.append(_make_batch(system_prompt, current, budget))
            current = [item]
        else:
            current = candidate
    if current:
        batches.append(_make_batch(system_prompt, current, budget))
    return batches


async def _run_batch(
    batch: Batch,
    system_prompt: str,
    taxonomy: Taxonomy,
    llm: LLMClient,
    *,
    acquire_call: AcquireCall | None,
    retry_reason: str | None,
) -> _Attempt:
    refs = [item.ref for item in batch.items]
    if acquire_call is not None and not await acquire_call():
        raise DailyCapReached()
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        response = await llm.complete_json(
            messages=build_messages(
                system_prompt, [item.rendered for item in batch.items], retry_reason=retry_reason
            ),
            schema=build_output_schema(taxonomy, refs),
            schema_name=SCHEMA_NAME,
            max_completion_tokens=batch.max_completion_tokens,
        )
    except LLMOutputError as exc:
        return _Attempt({}, {ref: exc.code for ref in refs}, True, llm.model, {}, elapsed())
    except LLMProviderError as exc:
        return _Attempt({}, {ref: exc.code for ref in refs}, False, llm.model, {}, elapsed())
    validation = validate_batch(
        response.content, taxonomy, refs, max_rationale_chars=MAX_RATIONALE_CHARS
    )
    return _Attempt(
        validation.proposals,
        validation.errors,
        True,
        response.model,
        dict(response.usage),
        elapsed(),
    )


def _outcome(
    item: PreparedDefect, attempt: _Attempt, used_batch: Batch, attempts: int
) -> AnalysisOutcome:
    proposal = attempt.proposals.get(item.ref)
    status: Literal["AI_PROPOSED", "NEEDS_MANUAL_RCA"] = (
        "AI_PROPOSED" if proposal else "NEEDS_MANUAL_RCA"
    )
    return AnalysisOutcome(
        status=status,
        proposal=proposal,
        failure_reason=None if proposal else attempt.errors.get(item.ref, "UNKNOWN"),
        llm_attempts=attempts,
        model=attempt.model,
        scrub_counts=item.scrub_counts,
        batch_size=len(used_batch.items),
        prompt_tokens_estimate=used_batch.prompt_tokens_estimate,
        max_completion_tokens=used_batch.max_completion_tokens,
        usage=attempt.usage,
        latency_ms=attempt.latency_ms,
    )


async def analyze_defects(
    defects: list[dict[str, Any]],
    taxonomy: Taxonomy,
    llm: LLMClient,
    *,
    budget: TokenBudget,
    acquire_call: AcquireCall | None = None,
    on_batch_done: Callable[[dict[int, AnalysisOutcome]], Awaitable[None]] | None = None,
) -> dict[int, AnalysisOutcome]:
    """Analyze defects in token-budgeted batches.

    ``on_batch_done`` receives each batch's outcomes as soon as they are final, so callers
    can persist progress. ``DailyCapReached`` propagates; outcomes already delivered stay.
    """
    system_prompt, prepared = prepare_defects(defects, taxonomy, budget)
    by_ref = {item.ref: item for item in prepared}
    results: dict[int, AnalysisOutcome] = {}

    for batch in plan_batches(system_prompt, prepared, budget):
        batch_outcomes: dict[int, AnalysisOutcome] = {}

        def record(
            item: PreparedDefect,
            attempt: _Attempt,
            used_batch: Batch,
            attempts: int,
            into: dict[int, AnalysisOutcome] = batch_outcomes,
        ) -> None:
            into[item.ado_id] = _outcome(item, attempt, used_batch, attempts)

        first = await _run_batch(
            batch, system_prompt, taxonomy, llm, acquire_call=acquire_call, retry_reason=None
        )
        retry_items: list[PreparedDefect] = []
        for item in batch.items:
            if item.ref in first.proposals or not first.retryable:
                record(item, first, batch, 1)
            else:
                retry_items.append(item)

        if retry_items:
            reasons = Counter(first.errors[item.ref] for item in retry_items)
            reason = reasons.most_common(1)[0][0]
            smaller = (
                max(1, len(retry_items) // 2) if reason == "LLM_OUTPUT_TRUNCATED" else None
            )
            try:
                for retry_batch in plan_batches(
                    system_prompt, retry_items, budget, max_per_batch=smaller
                ):
                    second = await _run_batch(
                        retry_batch,
                        system_prompt,
                        taxonomy,
                        llm,
                        acquire_call=acquire_call,
                        retry_reason=reason,
                    )
                    for item in retry_batch.items:
                        record(by_ref[item.ref], second, retry_batch, MAX_OUTPUT_ATTEMPTS)
            except DailyCapReached:
                # Keep what the first attempt already settled, then stop.
                results.update(batch_outcomes)
                if on_batch_done is not None and batch_outcomes:
                    await on_batch_done(batch_outcomes)
                raise

        results.update(batch_outcomes)
        if on_batch_done is not None:
            await on_batch_done(batch_outcomes)
    return results
