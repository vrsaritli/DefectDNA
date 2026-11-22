"""ai-knowledge-service worker: processes ``analyze_defects`` jobs.

Run with:  python -m services.ai_knowledge_service.app.worker

Logs carry job/project IDs, defect IDs, codes and counts only — never defect text,
prompts, model output text or secrets.
"""

import asyncio
import os
import socket
from typing import Any

import structlog
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from libs.common.config import Settings
from libs.common.database import create_mongo_client
from libs.common.logging import configure_logging
from libs.common.queue import JobQueue, MongoJobQueue
from libs.common.taxonomy import Taxonomy
from services.ai_knowledge_service.app.adapters.embedding import (
    EmbeddingAdapterError,
    EmbeddingClient,
    build_embedding_client,
)
from services.ai_knowledge_service.app.adapters.llm import GroqLLMClient, LLMClient
from services.ai_knowledge_service.app.analysis import (
    AI_PROPOSED,
    AnalysisOutcome,
    BudgetTooSmall,
    DailyCapReached,
    TokenBudget,
    analyze_defects,
)
from services.ai_knowledge_service.app.knowledge import (
    REEMBED_JOB,
    build_retriever,
    reembed_entries,
)
from services.ai_knowledge_service.app.repository import (
    consume_llm_call,
    ensure_aik_indexes,
    save_analysis,
)

JOB_TYPE = "analyze_defects"
LEASE_SECONDS = 600
MAX_JOB_ATTEMPTS = 3
MAX_STORED_ERRORS = 200

logger = structlog.get_logger()


class _LeaseLost(Exception):
    pass


async def process_analysis_job(
    job: dict[str, Any],
    *,
    database: AsyncIOMotorDatabase[dict[str, Any]],
    queue: JobQueue,
    llm: LLMClient,
    owner: str,
    settings: Settings,
    embedder: EmbeddingClient | None = None,
) -> None:
    job_id = str(job["_id"])
    payload = job.get("payload", {})
    project_id = str(payload["project_id"])
    taxonomy = Taxonomy.model_validate(payload["taxonomy"])
    defects: list[dict[str, Any]] = list(payload.get("defects", []))
    progress: dict[str, int] = {
        "total": len(defects),
        "done": 0,
        "failed": 0,
        "proposed": 0,
        "needs_manual": 0,
        "skipped": 0,
        **payload.get("progress", {}),
    }
    progress["total"] = len(defects)
    errors: list[dict[str, str]] = list(payload.get("errors", []))
    processed: set[int] = {int(item) for item in payload.get("processed_ids", [])}
    log = logger.bind(
        job_id=job_id,
        project_id=project_id,
        correlation_id=payload.get("correlation_id"),
    )
    log.info("analysis_job_started", total=len(defects), resumed=len(processed))
    await queue.heartbeat(
        job_id, owner, lease_seconds=LEASE_SECONDS, set_fields={"payload.progress": progress}
    )

    async def acquire_call() -> bool:
        return await consume_llm_call(database, project_id, settings.llm_daily_call_cap)

    lease_lost = False

    async def checkpoint(*, raise_on_loss: bool = True) -> None:
        nonlocal lease_lost
        progress["done"] = len(processed)
        still_owner = await queue.heartbeat(
            job_id,
            owner,
            lease_seconds=LEASE_SECONDS,
            set_fields={
                "payload.progress": progress,
                "payload.errors": errors[-MAX_STORED_ERRORS:],
                "payload.processed_ids": sorted(processed),
            },
        )
        if not still_owner:
            lease_lost = True
            if raise_on_loss:
                raise _LeaseLost()

    async def on_batch_done(outcomes: dict[int, AnalysisOutcome]) -> None:
        for ado_id, outcome in outcomes.items():
            item = f"ADO-{ado_id}"
            rca_id = await save_analysis(
                database,
                project_id=project_id,
                defect=defects_by_id[ado_id],
                outcome=outcome,
                taxonomy_version=taxonomy.version,
                job_id=job_id,
            )
            if rca_id is None:
                progress["skipped"] += 1
                errors.append({"item": item, "message": "ALREADY_REVIEWED"})
            elif outcome.status == AI_PROPOSED:
                progress["proposed"] += 1
            else:
                progress["needs_manual"] += 1
                progress["failed"] += 1
                errors.append(
                    {"item": item, "message": f"NEEDS_MANUAL_RCA: {outcome.failure_reason}"}
                )
            log.info(
                "defect_analyzed",
                defect_id=ado_id,
                status=outcome.status,
                failure_reason=outcome.failure_reason,
                llm_attempts=outcome.llm_attempts,
                batch_size=outcome.batch_size,
                prompt_tokens_estimate=outcome.prompt_tokens_estimate,
                max_completion_tokens=outcome.max_completion_tokens,
                batch_tokens=outcome.usage.get("total_tokens"),
                latency_ms=outcome.latency_ms,
                scrubbed=outcome.scrub_counts,
            )
            processed.add(ado_id)
        await checkpoint()

    defects_by_id = {int(defect["ado_id"]): defect for defect in defects}
    pending = [defect for ado_id, defect in defects_by_id.items() if ado_id not in processed]
    try:
        await analyze_defects(
            pending,
            taxonomy,
            llm,
            budget=TokenBudget.from_settings(settings),
            acquire_call=acquire_call,
            on_batch_done=on_batch_done,
            retriever=build_retriever(database, embedder, settings, project_id),
        )
    except _LeaseLost:
        log.warning("analysis_job_lease_lost", done=progress["done"])
        return
    except DailyCapReached:
        log.warning("llm_daily_cap_reached", cap=settings.llm_daily_call_cap)
        for ado_id in defects_by_id:
            if ado_id not in processed:
                errors.append({"item": f"ADO-{ado_id}", "message": "LLM_DAILY_CAP_REACHED"})
                progress["failed"] += 1
                processed.add(ado_id)
        await checkpoint(raise_on_loss=False)
    except BudgetTooSmall:
        log.error("taxonomy_exceeds_token_budget", budget=settings.llm_request_token_budget)
        for ado_id in defects_by_id:
            if ado_id not in processed:
                errors.append({"item": f"ADO-{ado_id}", "message": "PROMPT_EXCEEDS_TOKEN_BUDGET"})
                progress["failed"] += 1
                processed.add(ado_id)
        await checkpoint(raise_on_loss=False)
    if lease_lost:
        return
    await queue.complete(job_id, owner)
    await _drop_defect_text(database, job_id)
    log.info("analysis_job_finished", **progress)


async def process_reembed_job(
    job: dict[str, Any],
    *,
    database: AsyncIOMotorDatabase[dict[str, Any]],
    queue: JobQueue,
    embedder: EmbeddingClient,
    owner: str,
) -> None:
    job_id = str(job["_id"])
    payload = job.get("payload", {})
    project_id = payload.get("project_id")

    async def on_progress(done: int, total: int) -> None:
        await queue.heartbeat(
            job_id,
            owner,
            lease_seconds=LEASE_SECONDS,
            set_fields={"payload.progress": {"total": total, "done": done, "failed": 0}},
        )

    try:
        result = await reembed_entries(
            database,
            embedder,
            project_id=str(project_id) if project_id else None,
            force=bool(payload.get("force")),
            on_progress=on_progress,
        )
    except EmbeddingAdapterError as exc:
        await queue.fail(job_id, owner, exc.code)
        logger.error("reembed_failed", job_id=job_id, code=exc.code)
        return
    await queue.heartbeat(
        job_id,
        owner,
        lease_seconds=LEASE_SECONDS,
        set_fields={
            "payload.progress": {"total": result["total"], "done": result["embedded"], "failed": 0}
        },
    )
    await queue.complete(job_id, owner)
    logger.info("reembed_finished", job_id=job_id, project_id=project_id, **result)


async def _drop_defect_text(database: AsyncIOMotorDatabase[dict[str, Any]], job_id: str) -> None:
    """Defect text is only needed while a job can still run; remove it once terminal."""
    await database["jobs"].update_one(
        {"_id": ObjectId(job_id)},
        {"$unset": {"payload.defects": "", "payload.processed_ids": ""}},
    )


async def run_worker(poll_seconds: float = 2.0) -> None:
    settings = Settings()
    configure_logging("ai-knowledge-worker")
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is required for the analysis worker")
    client = create_mongo_client(settings.mongodb_uri)
    database = client[settings.mongodb_db]
    queue = MongoJobQueue(database)
    await queue.ensure_indexes()
    await ensure_aik_indexes(database)
    owner = os.environ.get("WORKER_ID", f"aik-worker-{socket.gethostname()}-{os.getpid()}")
    llm = GroqLLMClient(
        settings.groq_api_key,
        settings.groq_model,
        base_url=settings.groq_base_url,
        timeout_seconds=settings.groq_timeout_seconds,
        max_retries=settings.groq_max_retries,
        reasoning_effort=settings.groq_reasoning_effort or None,
    )
    embedder = build_embedding_client(settings)
    logger.info(
        "analysis_worker_started",
        worker_id=owner,
        model=settings.groq_model,
        rag="enabled" if embedder else "disabled (no COHERE_API_KEY)",
    )
    try:
        while True:
            job = await queue.claim(JOB_TYPE, owner, lease_seconds=LEASE_SECONDS)
            if job is None and embedder is not None:
                reembed = await queue.claim(REEMBED_JOB, owner, lease_seconds=LEASE_SECONDS)
                if reembed is not None:
                    try:
                        await process_reembed_job(
                            reembed, database=database, queue=queue, embedder=embedder, owner=owner
                        )
                    except Exception as exc:  # noqa: BLE001 - keep the worker alive
                        logger.error("reembed_failed", error=type(exc).__name__)
                        await queue.fail(str(reembed["_id"]), owner, type(exc).__name__)
                    continue
            if job is None:
                await asyncio.sleep(poll_seconds)
                continue
            job_id = str(job["_id"])
            if int(job.get("attempts", 1)) > MAX_JOB_ATTEMPTS:
                await queue.fail(job_id, owner, "MAX_ATTEMPTS_EXCEEDED")
                await _drop_defect_text(database, job_id)
                logger.error("analysis_job_abandoned", job_id=job_id)
                continue
            try:
                await process_analysis_job(
                    job,
                    database=database,
                    queue=queue,
                    llm=llm,
                    owner=owner,
                    settings=settings,
                    embedder=embedder,
                )
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                logger.error("analysis_job_failed", job_id=job_id, error=type(exc).__name__)
                await queue.fail(job_id, owner, type(exc).__name__)
                await _drop_defect_text(database, job_id)
    finally:
        await llm.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
