"""Operational metrics for one project (read-only).

Covers the Architecture §26 signals this service can see: job outcomes, provider errors
and latency, LLM/embedding usage against daily caps, NEEDS_MANUAL_RCA share, average
confidence, AI agreement and knowledge-base health.
"""

import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from libs.common.config import Settings

_PROVIDER_PREFIXES = ("LLM_", "EMBEDDING_")


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _aware(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def collect_metrics(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    settings: Settings,
    hours: int = 24,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)

    analyzed = await database["rca_analyses"].find(
        {"project_id": project_id, "analyzed_at": {"$gte": since}},
        {"status": 1, "failure_reason": 1, "proposed.confidence": 1, "llm_batch.latency_ms": 1,
         "retrieval_status": 1},
    ).to_list(length=None)
    reasons = Counter(
        str(doc.get("failure_reason")) for doc in analyzed if doc.get("status") == "NEEDS_MANUAL_RCA"
    )
    confidences = [
        float(doc["proposed"]["confidence"])
        for doc in analyzed
        if isinstance(doc.get("proposed"), dict) and doc["proposed"].get("confidence") is not None
    ]
    latencies = [
        int(doc["llm_batch"]["latency_ms"])
        for doc in analyzed
        if isinstance(doc.get("llm_batch"), dict) and doc["llm_batch"].get("latency_ms")
    ]
    needs_manual = sum(reasons.values())

    reviewed = await database["rca_analyses"].find(
        {
            "project_id": project_id,
            "reviewed_at": {"$gte": since},
            "review_source": {"$ne": "SEED_IMPORT"},
        },
        {"status": 1},
    ).to_list(length=None)
    approved = sum(1 for doc in reviewed if doc.get("status") == "APPROVED")

    day = now.date().isoformat()
    llm_doc = await database["llm_usage"].find_one({"_id": f"{project_id}:{day}"})
    embed_doc = await database["llm_usage"].find_one({"_id": f"embed:{project_id}:{day}"})

    active = await database["knowledge_entries"].count_documents(
        {"project_id": project_id, "status": "ACTIVE"}
    )
    pending = await database["knowledge_entries"].count_documents(
        {"project_id": project_id, "status": "PENDING_EMBEDDING"}
    )

    jobs = await database["jobs"].find(
        {"payload.project_id": project_id, "updated_at": {"$gte": since}},
        {"type": 1, "status": 1, "error": 1, "updated_at": 1, "lease_expires_at": 1},
    ).to_list(length=None)
    by_type: dict[str, Counter[str]] = {}
    failed_jobs = []
    stuck = 0
    for job in jobs:
        by_type.setdefault(str(job.get("type")), Counter())[str(job.get("status"))] += 1
        if job.get("status") == "FAILED":
            failed_jobs.append(
                {
                    "job_id": str(job["_id"]),
                    "type": job.get("type"),
                    "error": job.get("error"),
                    "updated_at": job.get("updated_at"),
                }
            )
        lease = _aware(job.get("lease_expires_at"))
        if job.get("status") in {"CLAIMED", "RUNNING"} and lease is not None and lease < now:
            stuck += 1

    return {
        "project_id": project_id,
        "window_hours": hours,
        "generated_at": now,
        "analysis": {
            "analyzed": len(analyzed),
            "needs_manual": needs_manual,
            "needs_manual_rate": round(needs_manual / len(analyzed), 4) if analyzed else None,
            "failure_reasons": dict(reasons.most_common()),
            "provider_errors": sum(
                count for reason, count in reasons.items() if reason.startswith(_PROVIDER_PREFIXES)
            ),
            "mean_confidence": round(statistics.fmean(confidences), 4) if confidences else None,
            "llm_latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
            "retrieval": dict(Counter(str(doc.get("retrieval_status")) for doc in analyzed)),
        },
        "reviews": {
            "reviewed": len(reviewed),
            "approved_unchanged": approved,
            "corrected": len(reviewed) - approved,
            "ai_agreement_rate": round(approved / len(reviewed), 4) if reviewed else None,
        },
        "usage_today": {
            "llm_calls": int(llm_doc["calls"]) if llm_doc else 0,
            "llm_cap": settings.llm_daily_call_cap,
            "embed_calls": int(embed_doc["calls"]) if embed_doc else 0,
            "embed_cap": settings.embed_daily_call_cap,
        },
        "knowledge": {
            "active": active,
            "pending_embedding": pending,
            "rag_enabled": bool(settings.cohere_api_key),
        },
        "jobs": {
            "by_type": {key: dict(value) for key, value in by_type.items()},
            "failed": len(failed_jobs),
            "failed_recent": sorted(
                failed_jobs, key=lambda job: str(job.get("updated_at")), reverse=True
            )[:10],
            "stuck": stuck,
        },
    }
