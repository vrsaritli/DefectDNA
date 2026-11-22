"""Alert check for schedulers and monitors (Windows Task Scheduler, cron).

    python ops/check_alerts.py                 # every project, last 24 h
    python ops/check_alerts.py --project demo --hours 6

Prints one JSON document and exits 0 (ok), 1 (warnings) or 2 (critical), so any
monitoring tool can alert on the exit code until the platform's tooling is chosen
(Architecture §26, pending P6). Reads the database directly with the service's own
read-only queries; prints no defect text.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.common.config import Settings
from libs.common.database import create_mongo_client
from libs.common.ops import auth_metrics, evaluate_alerts, overall_status
from services.ai_knowledge_service.app.ops import collect_metrics

EXIT_CODES = {"ok": 0, "warning": 1, "critical": 2}


async def run(project: str | None, hours: int) -> dict[str, Any]:
    settings = Settings()
    client = create_mongo_client(settings.mongodb_uri)
    try:
        database = client[settings.mongodb_db]
        if project:
            projects = [project]
        else:
            projects = [str(doc["_id"]) async for doc in database["projects"].find({}, {"_id": 1})]
        auth = await auth_metrics(database, datetime.now(UTC) - timedelta(hours=hours))
        results = {}
        worst = "ok"
        for project_id in projects:
            metrics = await collect_metrics(database, project_id, settings, hours)
            metrics["auth"] = auth
            alerts = evaluate_alerts(metrics)
            status = overall_status(alerts)
            if EXIT_CODES[status] > EXIT_CODES[worst]:
                worst = status
            results[project_id] = {"status": status, "alerts": [a.to_dict() for a in alerts]}
        return {
            "status": worst,
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "window_hours": hours,
            "projects": results,
        }
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="DefectDNA alert check")
    parser.add_argument("--project")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    report = asyncio.run(run(args.project, args.hours))
    print(json.dumps(report, indent=2, default=str))
    sys.exit(EXIT_CODES[report["status"]])


if __name__ == "__main__":
    main()
