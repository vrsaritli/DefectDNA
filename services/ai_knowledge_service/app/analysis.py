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
from services.ai_knowledge_service.app.prompts.rca_v3 import (
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
    estimate_tokens,
    render_record,
)

Retriever = Callable[[list[dict[str, Any]]], Awaitable[Any]]

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
    cases: list[dict[str, Any]] = field(default_factory=list)


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
    retrieval_status: str = "DISABLED"
    embedding_model: str | None = None
    retrieved_cases: list[dict[str, Any]] = field(default_factory=list)
    cited_cases: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Attempt:
    proposals: dict[str, RcaProposal]
    errors: dict[str, str]
    retryable: bool
    model: str
    usage: dict[str, int]
    latency_ms: int
    citations: dict[str, list[str]] = field(default_factory=dict)


def prepare_defects(
    defects: list[dict[str, Any]],
    taxonomy: Taxonomy,
    budget: TokenBudget,
    cases_by_id: dict[int, list[dict[str, Any]]] | None = None,
) -> tuple[str, list[PreparedDefect]]:
    system_prompt = build_system_prompt(taxonomy)
    base = estimate_prompt_tokens(system_prompt, [])
    if base + budget.output_per_defect + _MIN_DEFECT_TOKENS > budget.request_budget:
        raise BudgetTooSmall()
    per_defect_tokens = max(
        _MIN_DEFECT_TOKENS, budget.batch_target - base - budget.output_per_defect
    )
    prepared = []
    for defect in defects:
        ado_id = int(defect["ado_id"])
        cases = (cases_by_id or {}).get(ado_id, [])
        ref = defect_ref(ado_id)
        # Retrieved cases share the per-defect token allowance with the defect text.
        case_tokens = estimate_tokens(render_record(ref, {}, cases)) if cases else 0
        available = max(_MIN_DEFECT_TOKENS, per_defect_tokens - case_tokens)
        max_chars = max(600, int(available * CHARS_PER_TOKEN) - _RECORD_JSON_OVERHEAD_CHARS)
        record, counts = compose_defect_record(defect, max_chars)
        prepared.append(
            PreparedDefect(
                ado_id=ado_id,
                ref=ref,
                rendered=render_record(ref, record, cases),
                scrub_counts=counts,
                cases=cases,
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
            schema=build_output_schema(
                taxonomy, refs, max((len(item.cases) for item in batch.items), default=0)
            ),
            schema_name=SCHEMA_NAME,
            max_completion_tokens=batch.max_completion_tokens,
        )
    except LLMOutputError as exc:
        return _Attempt({}, {ref: exc.code for ref in refs}, True, llm.model, {}, elapsed())
    except LLMProviderError as exc:
        return _Attempt({}, {ref: exc.code for ref in refs}, False, llm.model, {}, elapsed())
    validation = validate_batch(
        response.content,
        taxonomy,
        refs,
        max_rationale_chars=MAX_RATIONALE_CHARS,
        case_counts={item.ref: len(item.cases) for item in batch.items},
    )
    return _Attempt(
        validation.proposals,
        validation.errors,
        True,
        response.model,
        dict(response.usage),
        elapsed(),
        validation.citations,
    )


def _outcome(
    item: PreparedDefect, attempt: _Attempt, used_batch: Batch, attempts: int
) -> AnalysisOutcome:
    proposal = attempt.proposals.get(item.ref)
    status: Literal["AI_PROPOSED", "NEEDS_MANUAL_RCA"] = (
        "AI_PROPOSED" if proposal else "NEEDS_MANUAL_RCA"
    )
    cited_refs = set(attempt.citations.get(item.ref, [])) if proposal else set()
    return AnalysisOutcome(
        retrieved_cases=item.cases,
        cited_cases=[case for case in item.cases if case["ref"] in cited_refs],
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
    retriever: Retriever | None = None,
) -> dict[int, AnalysisOutcome]:
    """Analyze defects in token-budgeted batches.

    ``retriever`` (RAG) returns similar confirmed cases per defect; when it is absent or
    fails, analysis proceeds without cases and the status is recorded on each outcome.
    ``on_batch_done`` receives each batch's outcomes as soon as they are final, so callers
    can persist progress. ``DailyCapReached`` propagates; outcomes already delivered stay.
    """
    retrieval_status = "DISABLED"
    embedding_model: str | None = None
    cases_by_id: dict[int, list[dict[str, Any]]] = {}
    if retriever is not None and defects:
        retrieval = await retriever(defects)
        retrieval_status = retrieval.status
        embedding_model = retrieval.embedding_model
        cases_by_id = retrieval.cases
    system_prompt, prepared = prepare_defects(defects, taxonomy, budget, cases_by_id)
    by_ref = {item.ref: item for item in prepared}
    results: dict[int, AnalysisOutcome] = {}

    async def deliver(outcomes: dict[int, AnalysisOutcome]) -> None:
        for outcome in outcomes.values():
            outcome.retrieval_status = retrieval_status
            outcome.embedding_model = embedding_model
        results.update(outcomes)
        if on_batch_done is not None and outcomes:
            await on_batch_done(outcomes)

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
                await deliver(batch_outcomes)
                raise

        await deliver(batch_outcomes)
    return results
