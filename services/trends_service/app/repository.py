"""trend_facts storage and aggregations (owned by the trends-service)."""

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, cast

from motor.motor_asyncio import AsyncIOMotorDatabase

from libs.common.trends import DIMENSIONS, TrendFact

_FILTER_FIELDS = ("module", "release", "root_cause", "sdlc_phase", "testing_gap", "severity")


async def ensure_trend_indexes(database: AsyncIOMotorDatabase[dict[str, Any]]) -> None:
    facts = database["trend_facts"]
    await facts.create_index(
        [("project_id", 1), ("defect_id", 1)], unique=True, name="uq_trend_project_defect"
    )
    await facts.create_index(
        [("project_id", 1), ("module", 1), ("release", 1), ("month", 1)],
        name="ix_trend_project_module_release_month",
    )


async def upsert_fact(database: AsyncIOMotorDatabase[dict[str, Any]], fact: TrendFact) -> None:
    now = datetime.now(UTC)
    await database["trend_facts"].update_one(
        {"project_id": fact.project_id, "defect_id": fact.defect_id},
        {"$set": {**fact.model_dump(), "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def replace_project_facts(
    database: AsyncIOMotorDatabase[dict[str, Any]], project_id: str, facts: list[TrendFact]
) -> int:
    """Rebuild: make the project's facts exactly equal to ``facts``."""
    keep = {fact.defect_id for fact in facts}
    for fact in facts:
        await upsert_fact(database, fact)
    await database["trend_facts"].delete_many(
        {"project_id": project_id, "defect_id": {"$nin": sorted(keep)}}
    )
    return len(facts)


def build_match(project_id: str, filters: dict[str, str | None]) -> dict[str, Any]:
    match: dict[str, Any] = {"project_id": project_id}
    for name in _FILTER_FIELDS:
        value = filters.get(name)
        if value:
            match[DIMENSIONS[name]] = value
    month_range: dict[str, str] = {}
    if filters.get("month_from"):
        month_range["$gte"] = str(filters["month_from"])
    if filters.get("month_to"):
        month_range["$lte"] = str(filters["month_to"])
    if month_range:
        match["month"] = month_range
    return match


async def _facts(
    database: AsyncIOMotorDatabase[dict[str, Any]], match: dict[str, Any], limit: int = 100_000
) -> list[dict[str, Any]]:
    cursor = database["trend_facts"].find(match, {"_id": 0, "created_at": 0, "updated_at": 0})
    return await cursor.to_list(length=limit)


async def aggregate(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    group_by: list[str],
    filters: dict[str, str | None],
) -> list[dict[str, Any]]:
    """Counts per group, each with a root-cause breakdown.

    Aggregation happens in the service: a project holds at most a few thousand facts
    (NFR-01), and this keeps the logic identical across MongoDB and test doubles.
    """
    groups: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
    for fact in await _facts(database, build_match(project_id, filters)):
        key = tuple(str(fact.get(DIMENSIONS[name]) or "Unknown") for name in group_by)
        groups[key][str(fact.get("root_cause_code"))] += 1
    rows = [
        {
            **dict(zip(group_by, key, strict=True)),
            "total": sum(counts.values()),
            "by_root_cause": dict(counts.most_common()),
        }
        for key, counts in groups.items()
    ]
    if "month" in group_by:
        rows.sort(key=lambda row: (str(row["month"]), -cast(int, row["total"])))
    else:
        rows.sort(key=lambda row: (-cast(int, row["total"]), tuple(str(row[n]) for n in group_by)))
    return rows


def _ranked(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


async def summary(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    filters: dict[str, str | None],
) -> dict[str, Any]:
    facts = await _facts(database, build_match(project_id, filters))
    by: dict[str, Counter[str]] = {name: Counter() for name in DIMENSIONS}
    by_month_root: dict[str, Counter[str]] = defaultdict(Counter)
    corrected = 0
    for fact in facts:
        for name, field in DIMENSIONS.items():
            by[name][str(fact.get(field) or "Unknown")] += 1
        by_month_root[str(fact.get("month") or "Unknown")][str(fact.get("root_cause_code"))] += 1
        corrected += int(fact.get("status") == "CORRECTED")

    all_facts = await _facts(database, {"project_id": project_id})
    return {
        "total": len(facts),
        "approved": len(facts) - corrected,
        "corrected": corrected,
        "ai_agreement_rate": round((len(facts) - corrected) / len(facts), 4) if facts else None,
        "by_root_cause": _ranked(by["root_cause"]),
        "by_module": _ranked(by["module"]),
        "by_release": _ranked(by["release"]),
        "by_sdlc_phase": _ranked(by["sdlc_phase"]),
        "by_testing_gap": _ranked(by["testing_gap"]),
        "by_severity": _ranked(by["severity"]),
        "by_month": [
            {"month": month, "total": sum(counts.values()), "by_root_cause": dict(counts)}
            for month, counts in sorted(by_month_root.items())
        ],
        "filters": {
            "modules": sorted({str(f.get("module") or "Unknown") for f in all_facts}),
            "releases": sorted({str(f.get("release") or "Unknown") for f in all_facts}),
            "months": sorted({str(f.get("month") or "Unknown") for f in all_facts}),
        },
    }


async def list_facts(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    filters: dict[str, str | None],
    limit: int,
) -> list[dict[str, Any]]:
    facts = await _facts(database, build_match(project_id, filters), limit)
    facts.sort(key=lambda fact: (str(fact.get("month")), int(fact.get("defect_id", 0))))
    return facts
