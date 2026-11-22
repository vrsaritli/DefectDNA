"""Versioned per-project RCA taxonomies (owned by the api-gateway)."""

from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from libs.common.taxonomy import Taxonomy, TaxonomyKind, TaxonomyLists, starter_taxonomy


class TaxonomyConflict(Exception):
    """Another update created the same version concurrently."""


async def ensure_taxonomy_indexes(database: AsyncIOMotorDatabase[dict[str, Any]]) -> None:
    await database["taxonomies"].create_index(
        [("project_id", 1), ("version", 1)], unique=True, name="uq_taxonomy_project_version"
    )
    await database["taxonomies"].create_index(
        [("project_id", 1), ("is_current", 1)], name="ix_taxonomy_project_current"
    )


def _to_model(doc: dict[str, Any]) -> Taxonomy:
    return Taxonomy.model_validate(
        {key: doc[key] for key in ("version", "root_causes", "sdlc_phases", "testing_gaps")}
    )


async def get_current_taxonomy(
    database: AsyncIOMotorDatabase[dict[str, Any]], project_id: str
) -> Taxonomy:
    """Current taxonomy; the starter taxonomy becomes version 1 on first use."""
    docs = (
        await database["taxonomies"]
        .find({"project_id": project_id, "is_current": True})
        .sort("version", -1)
        .limit(1)
        .to_list(length=1)
    )
    if docs:
        return _to_model(docs[0])
    starter = starter_taxonomy()
    now = datetime.now(UTC)
    try:
        await database["taxonomies"].insert_one(
            {
                "project_id": project_id,
                **starter.model_dump(mode="json"),
                "is_current": True,
                "created_by": None,
                "created_at": now,
            }
        )
    except DuplicateKeyError:
        existing = await database["taxonomies"].find_one(
            {"project_id": project_id, "version": starter.version}
        )
        if existing is not None:
            return _to_model(existing)
        raise
    return starter


async def get_taxonomy_version(
    database: AsyncIOMotorDatabase[dict[str, Any]], project_id: str, version: int
) -> Taxonomy | None:
    doc = await database["taxonomies"].find_one({"project_id": project_id, "version": version})
    return _to_model(doc) if doc else None


async def save_taxonomy_version(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    lists: TaxonomyLists,
    user_id: Any,
) -> Taxonomy:
    current = await get_current_taxonomy(database, project_id)
    new = Taxonomy.model_validate({**lists.model_dump(mode="json"), "version": current.version + 1})
    now = datetime.now(UTC)
    try:
        await database["taxonomies"].insert_one(
            {
                "project_id": project_id,
                **new.model_dump(mode="json"),
                "is_current": True,
                "created_by": user_id,
                "created_at": now,
            }
        )
    except DuplicateKeyError as exc:
        raise TaxonomyConflict() from exc
    await database["taxonomies"].update_many(
        {"project_id": project_id, "version": {"$ne": new.version}},
        {"$set": {"is_current": False}},
    )
    return new


def _coded(taxonomy: Taxonomy | None, kind: TaxonomyKind, code: Any) -> dict[str, Any] | None:
    if code is None:
        return None
    label = taxonomy.label(kind, code) if taxonomy else None
    return {"code": code, "label": label or code}


async def enrich_rca(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    project_id: str,
    rca: dict[str, Any],
    cache: dict[int, Taxonomy | None] | None = None,
) -> dict[str, Any]:
    """Shape an internal RCA record for the public API, adding taxonomy labels."""
    cache = cache if cache is not None else {}
    version = rca.get("taxonomy_version")
    taxonomy: Taxonomy | None = None
    if isinstance(version, int):
        if version not in cache:
            cache[version] = await get_taxonomy_version(database, project_id, version)
        taxonomy = cache[version]

    def classification(source: dict[str, Any] | None) -> dict[str, Any] | None:
        if not source:
            return None
        return {
            "root_cause": _coded(taxonomy, "root_causes", source.get("root_cause_code")),
            "sdlc_phase": _coded(taxonomy, "sdlc_phases", source.get("sdlc_phase_code")),
            "testing_gap": _coded(taxonomy, "testing_gaps", source.get("testing_gap_code")),
        }

    proposed = rca.get("proposed")
    proposed_view = None
    if proposed:
        proposed_view = {
            **(classification(proposed) or {}),
            "other_proposal": proposed.get("other_proposal"),
            "confidence": proposed.get("confidence"),
            "rationale": proposed.get("rationale"),
        }
    final = rca.get("final")
    snapshot = rca.get("defect_snapshot") or {}
    batch = rca.get("llm_batch") or {}
    return {
        "id": rca.get("id"),
        "project_id": rca.get("project_id"),
        "status": rca.get("status"),
        "version": rca.get("version"),
        "defect": {"ado_id": rca.get("defect_id"), **snapshot},
        "proposed": proposed_view,
        "final": {**(classification(final) or {}), "note": final.get("note")} if final else None,
        "failure_reason": rca.get("failure_reason"),
        "cited_cases": rca.get("cited_cases") or [],
        "taxonomy_version": version,
        "model": rca.get("model"),
        "llm_attempts": rca.get("llm_attempts"),
        "llm_batch": {
            "size": batch.get("size"),
            "usage": batch.get("usage") or {},
        },
        "scrub_counts": rca.get("scrub_counts") or {},
        "analyzed_at": rca.get("analyzed_at"),
        "reviewed_at": rca.get("reviewed_at"),
        "reviewer_id": rca.get("reviewer_id"),
    }
