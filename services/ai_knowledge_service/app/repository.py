"""Data access for collections owned by the ai-knowledge-service."""

import math
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from services.ai_knowledge_service.app.analysis import AnalysisOutcome

REVIEWED_STATUSES = frozenset({"APPROVED", "CORRECTED"})
RCA_STATUSES = frozenset({"AI_PROPOSED", "NEEDS_MANUAL_RCA", *REVIEWED_STATUSES})


async def ensure_aik_indexes(database: AsyncIOMotorDatabase[dict[str, Any]]) -> None:
    await database["rca_analyses"].create_index(
        [("project_id", 1), ("defect_id", 1), ("version", 1)],
        unique=True,
        name="uq_rca_project_defect_version",
    )
    await database["rca_analyses"].create_index(
        [("project_id", 1), ("status", 1), ("updated_at", -1)],
        name="ix_rca_project_status",
    )
    await database["llm_usage"].create_index("expires_at", expireAfterSeconds=0)


async def latest_by_defect(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    defect_ids: list[int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Latest RCA version per defect (optionally limited to the given defect IDs)."""
    match: dict[str, Any] = {"project_id": project_id}
    if defect_ids is not None:
        match["defect_id"] = {"$in": defect_ids}
    cursor = database["rca_analyses"].find(
        match, {"defect_id": 1, "version": 1, "status": 1}
    )
    latest: dict[int, dict[str, Any]] = {}
    async for doc in cursor:
        current = latest.get(doc["defect_id"])
        if current is None or doc["version"] > current["version"]:
            latest[doc["defect_id"]] = doc
    return latest


async def find_latest_for_defect(
    database: AsyncIOMotorDatabase[dict[str, Any]], project_id: str, defect_id: int
) -> dict[str, Any] | None:
    docs = (
        await database["rca_analyses"]
        .find({"project_id": project_id, "defect_id": defect_id})
        .sort("version", -1)
        .limit(1)
        .to_list(length=1)
    )
    return docs[0] if docs else None


def _closed_month(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    if isinstance(value, str) and len(value) >= 7:
        return value[:7]
    return None


async def save_analysis(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    project_id: str,
    defect: dict[str, Any],
    outcome: AnalysisOutcome,
    taxonomy_version: int,
    job_id: str | None,
) -> ObjectId | None:
    """Store an AI result. Returns None when the defect already has a reviewed RCA.

    Re-analysis overwrites the latest unreviewed version in place, so retries and
    repeated jobs never create duplicates; reviewed RCAs are never overwritten here
    (re-opening is a Phase 5 review action).
    """
    defect_id = int(defect["ado_id"])
    latest = await find_latest_for_defect(database, project_id, defect_id)
    if latest is not None and latest["status"] in REVIEWED_STATUSES:
        return None
    version = latest["version"] if latest is not None else 1
    now = datetime.now(UTC)
    result = await database["rca_analyses"].find_one_and_update(
        {"project_id": project_id, "defect_id": defect_id, "version": version},
        {
            "$set": {
                "status": outcome.status,
                "proposed": outcome.proposal.to_dict() if outcome.proposal else None,
                "failure_reason": outcome.failure_reason,
                "final": None,
                "cited_cases": [],
                "retrieved_cases": [],
                "taxonomy_version": taxonomy_version,
                "model": {
                    "llm": outcome.model,
                    "prompt_version": outcome.prompt_version,
                    "embedding": None,
                },
                "llm_attempts": outcome.llm_attempts,
                "llm_batch": {
                    "size": outcome.batch_size,
                    "prompt_tokens_estimate": outcome.prompt_tokens_estimate,
                    "max_completion_tokens": outcome.max_completion_tokens,
                    "usage": outcome.usage,
                },
                "scrub_counts": outcome.scrub_counts,
                "job_id": ObjectId(job_id) if job_id and ObjectId.is_valid(job_id) else None,
                "reviewer_id": None,
                "reviewed_at": None,
                "defect_snapshot": {
                    "title": str(defect.get("title") or "")[:300],
                    "module": str(defect.get("module") or ""),
                    "release": str(defect.get("release") or ""),
                    "severity": str(defect.get("severity") or ""),
                    "closed_month": _closed_month(defect.get("closed_date")),
                },
                "analyzed_at": now,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return cast(ObjectId, result["_id"]) if result is not None else None


async def consume_llm_call(
    database: AsyncIOMotorDatabase[dict[str, Any]], project_id: str, daily_cap: int
) -> bool:
    """Count one LLM call against the project's daily cap; False once the cap is reached."""
    today = datetime.now(UTC).date()
    doc = await database["llm_usage"].find_one_and_update(
        {"_id": f"{project_id}:{today.isoformat()}"},
        {
            "$inc": {"calls": 1},
            "$setOnInsert": {
                "project_id": project_id,
                "day": today.isoformat(),
                "expires_at": datetime.now(UTC) + timedelta(days=30),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc is not None and int(doc["calls"]) <= daily_cap


def serialize_rca(doc: dict[str, Any]) -> dict[str, Any]:
    result = dict(doc)
    result["id"] = str(result.pop("_id"))
    for key in ("job_id", "reviewer_id"):
        if isinstance(result.get(key), ObjectId):
            result[key] = str(result[key])
    return result


def build_trend_fact(rca: dict[str, Any]) -> dict[str, Any] | None:
    """Trend-fact payload for a reviewed RCA (None if it is not reviewed)."""
    final = rca.get("final") or {}
    if rca.get("status") not in REVIEWED_STATUSES or not final.get("root_cause_code"):
        return None
    snapshot = rca.get("defect_snapshot") or {}
    proposed = rca.get("proposed") or {}
    reviewed_at = rca.get("reviewed_at")
    return {
        "project_id": rca["project_id"],
        "defect_id": int(rca["defect_id"]),
        "rca_id": str(rca["_id"]),
        "status": rca["status"],
        "module": snapshot.get("module") or "Unknown",
        "release": snapshot.get("release") or "Unknown",
        "month": snapshot.get("closed_month") or "Unknown",
        "severity": snapshot.get("severity") or "Unknown",
        "root_cause_code": final["root_cause_code"],
        "sdlc_phase_code": final["sdlc_phase_code"],
        "testing_gap_code": final["testing_gap_code"],
        "ai_root_cause_code": proposed.get("root_cause_code"),
        "reviewed_at": reviewed_at.isoformat() if isinstance(reviewed_at, datetime) else reviewed_at,
    }


async def reviewed_trend_facts(
    database: AsyncIOMotorDatabase[dict[str, Any]], project_id: str
) -> list[dict[str, Any]]:
    """Facts for the latest version of every reviewed RCA in the project."""
    latest: dict[int, dict[str, Any]] = {}
    async for doc in database["rca_analyses"].find({"project_id": project_id}):
        current = latest.get(doc["defect_id"])
        if current is None or doc["version"] > current["version"]:
            latest[doc["defect_id"]] = doc
    facts = [build_trend_fact(doc) for doc in latest.values()]
    return [fact for fact in facts if fact is not None]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


async def retrieve_knowledge(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    project_id: str,
    query_embedding: list[float],
    top_k: int,
    threshold: float,
) -> list[dict[str, Any]]:
    """Project-scoped nearest approved knowledge entries (cosine similarity).

    In-process fallback for development and tests; production retrieval should use the
    Atlas ``$vectorSearch`` index created for Phase 5.
    """
    scored = []
    async for entry in database["knowledge_entries"].find(
        {"project_id": project_id, "status": "ACTIVE"}
    ):
        score = _cosine(list(entry.get("embedding") or []), query_embedding)
        if score >= threshold:
            entry.pop("embedding", None)
            entry["score"] = round(score, 6)
            scored.append(entry)
    scored.sort(key=lambda entry: -entry["score"])
    return scored[:top_k]