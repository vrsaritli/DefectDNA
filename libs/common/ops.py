"""Operational alert rules shared by the gateway's Operations view and the CLI check.

Monitoring tooling is still pending (Architecture §26), so alerts are computed from data
the services already hold and exposed two ways: ``GET /api/v1/projects/{id}/ops`` for
humans and ``ops/check_alerts.py`` (exit code 0/1/2) for any scheduler or monitor.
"""

from dataclasses import asdict, dataclass
from typing import Any, Literal

Severity = Literal["warning", "critical"]


@dataclass(frozen=True)
class Alert:
    code: str
    severity: Severity
    message: str
    value: float | int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


THRESHOLDS: dict[str, float] = {
    "needs_manual_rate_warning": 0.25,
    "needs_manual_min_sample": 5,
    "provider_errors_critical": 5,
    "usage_warning_ratio": 0.8,
    "latency_p95_warning_ms": 60_000,
    "login_failures_warning": 20,
}


def _ratio(used: Any, cap: Any) -> float | None:
    try:
        return float(used) / float(cap) if cap else None
    except (TypeError, ValueError):
        return None


def evaluate_alerts(metrics: dict[str, Any]) -> list[Alert]:
    """Turn a metrics snapshot (ai-knowledge ``collect_metrics`` + auth stats) into alerts."""
    alerts: list[Alert] = []
    jobs = metrics.get("jobs", {})
    analysis = metrics.get("analysis", {})
    usage = metrics.get("usage_today", {})
    knowledge = metrics.get("knowledge", {})
    auth = metrics.get("auth", {})

    if jobs.get("stuck", 0):
        alerts.append(
            Alert("JOBS_STUCK", "critical", "Jobs are past their lease without finishing", jobs["stuck"])
        )
    if jobs.get("failed", 0):
        alerts.append(Alert("JOBS_FAILED", "warning", "Background jobs failed", jobs["failed"]))

    errors = analysis.get("provider_errors", 0)
    if errors:
        severity: Severity = (
            "critical" if errors >= THRESHOLDS["provider_errors_critical"] else "warning"
        )
        alerts.append(
            Alert("PROVIDER_ERRORS", severity, "Groq/Cohere errors forced manual RCAs", errors)
        )
    analyzed = analysis.get("analyzed", 0)
    rate = analysis.get("needs_manual_rate")
    if (
        rate is not None
        and analyzed >= THRESHOLDS["needs_manual_min_sample"]
        and rate > THRESHOLDS["needs_manual_rate_warning"]
    ):
        alerts.append(
            Alert("NEEDS_MANUAL_RATE_HIGH", "warning", "Share of NEEDS_MANUAL_RCA is high", rate)
        )
    p95 = (analysis.get("llm_latency_ms") or {}).get("p95")
    if p95 is not None and p95 > THRESHOLDS["latency_p95_warning_ms"]:
        alerts.append(Alert("LLM_LATENCY_HIGH", "warning", "LLM batch latency p95 is high", p95))

    for kind, label in (("llm", "LLM"), ("embed", "Embedding")):
        ratio = _ratio(usage.get(f"{kind}_calls"), usage.get(f"{kind}_cap"))
        if ratio is None:
            continue
        if ratio >= 1:
            alerts.append(
                Alert(f"{kind.upper()}_CAP_REACHED", "critical", f"{label} daily cap reached", ratio)
            )
        elif ratio >= THRESHOLDS["usage_warning_ratio"]:
            alerts.append(
                Alert(f"{kind.upper()}_CAP_NEAR", "warning", f"{label} daily cap above 80%", ratio)
            )

    if knowledge.get("rag_enabled") and knowledge.get("pending_embedding", 0):
        alerts.append(
            Alert(
                "EMBEDDINGS_PENDING",
                "warning",
                "Knowledge entries wait for embedding; run the re-embed job",
                knowledge["pending_embedding"],
            )
        )
    if auth.get("lockouts", 0):
        alerts.append(Alert("ACCOUNT_LOCKOUTS", "warning", "Accounts were locked out", auth["lockouts"]))
    if auth.get("login_failures", 0) >= THRESHOLDS["login_failures_warning"]:
        alerts.append(
            Alert("LOGIN_FAILURES_HIGH", "warning", "Many failed sign-ins", auth["login_failures"])
        )
    return alerts


def overall_status(alerts: list[Alert]) -> str:
    if any(alert.severity == "critical" for alert in alerts):
        return "critical"
    return "warning" if alerts else "ok"


async def auth_metrics(database: Any, since: Any) -> dict[str, int]:
    """Sign-in health from the gateway's append-only audit log."""
    failures = await database["audit_log"].count_documents(
        {"action": "LOGIN_FAILED", "ts": {"$gte": since}}
    )
    lockouts = await database["audit_log"].count_documents(
        {"action": "LOGIN_LOCKED", "ts": {"$gte": since}}
    )
    return {"login_failures": failures, "lockouts": lockouts}
