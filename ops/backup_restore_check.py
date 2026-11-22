"""Restore drill: export a project's data, restore it into a scratch database, compare.

    python ops/backup_restore_check.py --project demo                 # dry run: counts only
    python ops/backup_restore_check.py --project demo --execute       # full drill
    python ops/backup_restore_check.py --project demo --execute --export-dir backups/

Atlas continuous backups remain the primary backup (Architecture §16). This drill proves
the data can be read back and restored: it copies every collection document of one
project into ``<MONGODB_DB>_restore_check`` (or ``--target-db``), verifies document
counts and a content checksum per collection, then drops the scratch database unless
``--keep`` is given. It never writes to the live database.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bson import json_util

from libs.common.config import Settings
from libs.common.database import create_mongo_client

PROJECT_COLLECTIONS = (
    "defects",
    "import_batches",
    "rca_analyses",
    "knowledge_entries",
    "trend_facts",
    "taxonomies",
    "user_project_roles",
)


def checksum(docs: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for doc in sorted(docs, key=lambda item: str(item.get("_id"))):
        digest.update(json_util.dumps(doc, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


async def drill(
    source: Any,
    target: Any,
    project_id: str,
    *,
    execute: bool,
    export_dir: Path | None,
) -> dict[str, Any]:
    report: dict[str, Any] = {"project_id": project_id, "collections": {}, "ok": True}
    for name in PROJECT_COLLECTIONS:
        docs = await source[name].find({"project_id": project_id}).to_list(length=None)
        entry: dict[str, Any] = {"source_count": len(docs)}
        if export_dir is not None:
            export_dir.mkdir(parents=True, exist_ok=True)
            (export_dir / f"{project_id}.{name}.json").write_text(
                json_util.dumps(docs), encoding="utf-8"
            )
        if execute:
            await target[name].delete_many({"project_id": project_id})
            if docs:
                await target[name].insert_many(docs)
            restored = await target[name].find({"project_id": project_id}).to_list(length=None)
            entry["restored_count"] = len(restored)
            entry["checksum_match"] = checksum(docs) == checksum(restored)
            entry["ok"] = entry["restored_count"] == len(docs) and entry["checksum_match"]
            report["ok"] = report["ok"] and entry["ok"]
        report["collections"][name] = entry
    return report


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    target_db = args.target_db or f"{settings.mongodb_db}_restore_check"
    if target_db == settings.mongodb_db:
        raise SystemExit("Refusing to restore into the live database")
    client = create_mongo_client(settings.mongodb_uri)
    try:
        report = await drill(
            client[settings.mongodb_db],
            client[target_db],
            args.project,
            execute=args.execute,
            export_dir=Path(args.export_dir) if args.export_dir else None,
        )
        if args.execute and not args.keep:
            await client.drop_database(target_db)
        report.update(
            {
                "target_db": target_db if args.execute else None,
                "executed": args.execute,
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        return report
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="DefectDNA restore drill")
    parser.add_argument("--project", required=True)
    parser.add_argument("--execute", action="store_true", help="Restore into the scratch database")
    parser.add_argument("--target-db")
    parser.add_argument("--export-dir", help="Also write a JSON export per collection")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch database")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
