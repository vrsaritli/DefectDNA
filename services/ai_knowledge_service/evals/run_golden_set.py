"""Run the RCA golden set through the production pipeline and write a report.

Uses exactly the worker's path (scrub -> token-budgeted batches -> Groq -> validate ->
one retry), so the score reflects what users get. Run it on every prompt, model or
taxonomy change (NFR-06).

Usage:
  python -m services.ai_knowledge_service.evals.run_golden_set
  python -m services.ai_knowledge_service.evals.run_golden_set --reasoning-effort low
"""

import argparse
import asyncio
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from libs.common.config import Settings
from libs.common.taxonomy import TAXONOMY_KINDS, Taxonomy, starter_taxonomy
from services.ai_knowledge_service.app.adapters.llm import GroqLLMClient, LLMClient, LLMResponse
from services.ai_knowledge_service.app.analysis import (
    AI_PROPOSED,
    AnalysisOutcome,
    TokenBudget,
    analyze_defects,
)
from services.ai_knowledge_service.app.pipeline.validate import (
    ProposalValidationError,
    validate_item,
)
from services.ai_knowledge_service.app.prompts.rca_v1 import (
    MAX_RATIONALE_CHARS,
    PROMPT_VERSION,
)

EVALS_DIR = Path(__file__).resolve().parent
_FIELDS = {
    "root_cause_code": "root_causes",
    "sdlc_phase_code": "sdlc_phases",
    "testing_gap_code": "testing_gaps",
}
HIGH_CONFIDENCE = 0.8


@dataclass
class RecordingLLM:
    """Proxy that records per-request token usage (no prompt or output text)."""

    inner: LLMClient
    requests: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""

    def __post_init__(self) -> None:
        self.model = self.inner.model

    async def complete_json(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        schema_name: str,
        max_completion_tokens: int | None = None,
    ) -> LLMResponse:
        refs = schema["properties"]["results"]["items"]["properties"]["ref"]["enum"]
        entry: dict[str, Any] = {"batch_size": len(refs), "max_completion_tokens": max_completion_tokens}
        try:
            response = await self.inner.complete_json(
                messages=messages,
                schema=schema,
                schema_name=schema_name,
                max_completion_tokens=max_completion_tokens,
            )
        except Exception as exc:
            entry["error"] = getattr(exc, "code", type(exc).__name__)
            self.requests.append(entry)
            raise
        entry["usage"] = response.usage
        self.requests.append(entry)
        return response

    async def close(self) -> None:
        await self.inner.close()


def load_golden_set(path: Path, taxonomy: Taxonomy) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = data["cases"]
    for case in cases:
        for name, code in case["expected"].items():
            kind = _FIELDS[name]
            if code not in taxonomy.codes(kind):  # type: ignore[arg-type]
                raise ValueError(f"{case['id']}: expected {name}={code} is not in the taxonomy")
    return cases


def score(
    cases: list[dict[str, Any]],
    outcomes: dict[int, AnalysisOutcome],
    taxonomy: Taxonomy,
) -> dict[str, Any]:
    total = len(cases)
    proposed = [o for o in outcomes.values() if o.status == AI_PROPOSED and o.proposal]
    conforming = 0
    for outcome in proposed:
        assert outcome.proposal is not None
        body = outcome.proposal.to_dict()
        body["other_proposal"] = body["other_proposal"] or ""
        try:
            validate_item(body, taxonomy, max_rationale_chars=MAX_RATIONALE_CHARS)
        except ProposalValidationError:
            continue
        conforming += 1

    per_field: dict[str, dict[str, int]] = defaultdict(lambda: {"scored": 0, "correct": 0})
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    mismatches: list[dict[str, Any]] = []
    confidences: list[float] = []
    high = {"n": 0, "correct": 0}
    low = {"n": 0, "correct": 0}
    all_correct = 0

    for case in cases:
        ado_id = int(case["defect"]["ado_id"])
        result = outcomes.get(ado_id)
        proposal = result.proposal if result else None
        predicted = proposal.to_dict() if proposal else {}
        case_correct = True
        for name, expected in case["expected"].items():
            got = predicted.get(name)
            per_field[name]["scored"] += 1
            if got == expected:
                per_field[name]["correct"] += 1
            else:
                case_correct = False
            if name == "root_cause_code":
                confusion[expected][got or "NEEDS_MANUAL_RCA"] += 1
        all_correct += int(case_correct)
        if proposal:
            confidences.append(proposal.confidence)
            bucket = high if proposal.confidence >= HIGH_CONFIDENCE else low
            bucket["n"] += 1
            bucket["correct"] += int(case_correct)
        if not case_correct:
            mismatches.append(
                {
                    "id": case["id"],
                    "expected": case["expected"],
                    "predicted": {name: predicted.get(name) for name in case["expected"]}
                    if proposal
                    else None,
                    "confidence": proposal.confidence if proposal else None,
                    "failure_reason": result.failure_reason if result else "NOT_ANALYZED",
                }
            )

    def rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "cases": total,
        "ai_proposed": len(proposed),
        "needs_manual_rca": total - len(proposed),
        "needs_manual_reasons": dict(
            Counter(
                o.failure_reason or "UNKNOWN" for o in outcomes.values() if o.status != AI_PROPOSED
            )
        ),
        "taxonomy_conformance": rate(conforming, len(proposed)),
        "match_rate": rate(all_correct, total),
        "per_field_accuracy": {
            name: rate(values["correct"], values["scored"]) for name, values in per_field.items()
        },
        "accuracy_on_proposed": rate(all_correct, len(proposed)),
        "mean_confidence": round(statistics.fmean(confidences), 4) if confidences else None,
        "calibration": {
            f"confidence>={HIGH_CONFIDENCE}": {
                "n": high["n"],
                "accuracy": rate(high["correct"], high["n"]),
            },
            f"confidence<{HIGH_CONFIDENCE}": {"n": low["n"], "accuracy": rate(low["correct"], low["n"])},
        },
        "retried_items": sum(1 for o in outcomes.values() if o.llm_attempts > 1),
        "root_cause_confusion": {key: dict(value) for key, value in sorted(confusion.items())},
        "mismatches": mismatches,
    }


def token_stats(requests: list[dict[str, Any]], budget: TokenBudget) -> dict[str, Any]:
    ok = [r for r in requests if "usage" in r]
    totals = [int(r["usage"].get("total_tokens", 0)) for r in ok]
    prompts = [int(r["usage"].get("prompt_tokens", 0)) for r in ok]
    completions = [int(r["usage"].get("completion_tokens", 0)) for r in ok]
    return {
        "requests": len(requests),
        "failed_requests": len(requests) - len(ok),
        "batch_sizes": dict(Counter(r["batch_size"] for r in requests)),
        "request_budget": budget.request_budget,
        "batch_target": budget.batch_target,
        "total_tokens": {
            "sum": sum(totals),
            "mean": round(statistics.fmean(totals)) if totals else None,
            "max": max(totals) if totals else None,
            "min": min(totals) if totals else None,
        },
        "prompt_tokens_mean": round(statistics.fmean(prompts)) if prompts else None,
        "completion_tokens_mean": round(statistics.fmean(completions)) if completions else None,
        "requests_over_budget": sum(1 for total in totals if total > budget.request_budget),
    }


def render_markdown(report: dict[str, Any]) -> str:
    s = report["scores"]
    t = report["tokens"]
    lines = [
        f"# Golden-set report — {report['prompt_version']} / {report['model']}",
        "",
        (
            f"- Run: {report['run_at']} · reasoning effort `{report['reasoning_effort']}` · "
            f"taxonomy v{report['taxonomy_version']}"
        ),
        (
            f"- Golden set: `{report['golden_set']}` ({s['cases']} cases; scored: "
            f"{', '.join(report['scored_fields'])})"
        ),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Match rate (all scored fields) | {_pct(s['match_rate'])} |",
    ]
    for name, value in s["per_field_accuracy"].items():
        lines.append(f"| Accuracy — {name} | {_pct(value)} |")
    lines += [
        f"| AI proposals / NEEDS_MANUAL_RCA | {s['ai_proposed']} / {s['needs_manual_rca']} |",
        f"| Stored outputs conforming to taxonomy | {_pct(s['taxonomy_conformance'])} |",
        f"| Mean confidence | {s['mean_confidence']} |",
    ]
    for bucket, values in s["calibration"].items():
        lines.append(f"| Accuracy when {bucket} | {_pct(values['accuracy'])} (n={values['n']}) |")
    lines += [
        f"| Items needing a retry | {s['retried_items']} |",
        f"| Groq requests (batch sizes) | {t['requests']} ({t['batch_sizes']}) |",
        (
            f"| Tokens per request mean / max | {t['total_tokens']['mean']} / "
            f"{t['total_tokens']['max']} (ceiling {t['request_budget']}) |"
        ),
        f"| Requests over the ceiling | {t['requests_over_budget']} |",
        "",
        "## Root-cause confusion (expected → predicted)",
        "",
        "| Expected | Predicted counts |",
        "|---|---|",
    ]
    for expected, predicted in s["root_cause_confusion"].items():
        counts = ", ".join(f"{code} {n}" for code, n in sorted(predicted.items(), key=lambda kv: -kv[1]))
        lines.append(f"| {expected} | {counts} |")
    lines += ["", "## Mismatches", "", "| Case | Expected | Predicted | Confidence |", "|---|---|---|---|"]
    for miss in s["mismatches"]:
        expected = ", ".join(miss["expected"].values())
        predicted = (
            ", ".join(str(v) for v in miss["predicted"].values())
            if miss["predicted"]
            else f"NEEDS_MANUAL_RCA ({miss['failure_reason']})"
        )
        lines.append(f"| {miss['id']} | {expected} | {predicted} | {miss['confidence']} |")
    return "\n".join(lines) + "\n"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    if not settings.groq_api_key:
        raise SystemExit("GROQ_API_KEY is not configured")
    taxonomy = (
        Taxonomy.model_validate_json(Path(args.taxonomy).read_text(encoding="utf-8"))
        if args.taxonomy
        else starter_taxonomy()
    )
    cases = load_golden_set(Path(args.golden_set), taxonomy)
    if args.limit:
        cases = cases[: args.limit]
    budget = TokenBudget.from_settings(settings)
    llm = RecordingLLM(
        GroqLLMClient(
            settings.groq_api_key,
            settings.groq_model,
            base_url=settings.groq_base_url,
            timeout_seconds=settings.groq_timeout_seconds,
            max_retries=max(settings.groq_max_retries, 6),
            reasoning_effort=args.reasoning_effort,
        )
    )
    started = datetime.now(UTC)
    try:
        outcomes = await analyze_defects(
            [case["defect"] for case in cases], taxonomy, llm, budget=budget
        )
    finally:
        await llm.close()
    scored_fields = sorted({name for case in cases for name in case["expected"]})
    return {
        "run_at": started.isoformat(timespec="seconds"),
        "duration_seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
        "golden_set": Path(args.golden_set).name,
        "model": settings.groq_model,
        "prompt_version": PROMPT_VERSION,
        "reasoning_effort": args.reasoning_effort,
        "taxonomy_version": taxonomy.version,
        "taxonomy_kinds": list(TAXONOMY_KINDS),
        "scored_fields": scored_fields,
        "scores": score(cases, outcomes, taxonomy),
        "tokens": token_stats(llm.requests, budget),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the RCA golden set")
    parser.add_argument("--golden-set", default=str(EVALS_DIR / "golden_set.json"))
    parser.add_argument("--taxonomy", help="Taxonomy JSON (defaults to the starter taxonomy)")
    parser.add_argument("--reasoning-effort", default=Settings().groq_reasoning_effort)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out-dir", default=str(EVALS_DIR / "reports"))
    args = parser.parse_args()

    report = asyncio.run(run(args))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-{PROMPT_VERSION}-{args.reasoning_effort}"
    (out_dir / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8")
    s = report["scores"]
    print(
        f"match rate {_pct(s['match_rate'])} · proposals {s['ai_proposed']}/{s['cases']} · "
        f"conformance {_pct(s['taxonomy_conformance'])} · "
        f"tokens/request max {report['tokens']['total_tokens']['max']} -> {out_dir / stem}.md"
    )


if __name__ == "__main__":
    main()
