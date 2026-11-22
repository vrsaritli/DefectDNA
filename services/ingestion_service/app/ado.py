from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx


_FIELDS = (
    "System.Id",
    "System.Title",
    "System.Description",
    "Microsoft.VSTS.TCM.ReproSteps",
    "System.AreaPath",
    "System.IterationPath",
    "Microsoft.VSTS.Common.Severity",
    "Microsoft.VSTS.Common.Priority",
    "System.State",
    "System.CreatedDate",
    "Microsoft.VSTS.Common.ClosedDate",
    "System.Tags",
)


class AdoDefectSource:
    def __init__(
        self,
        organization_url: str,
        project: str,
        pat: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.organization_url = organization_url.rstrip("/")
        parsed_url = urlparse(self.organization_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ValueError("ADO organization URL must be an HTTPS URL without user info")
        self.project = project
        self.client = httpx.AsyncClient(
            auth=httpx.BasicAuth("", pat),
            timeout=httpx.Timeout(20.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch_closed_defects(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        project = self.project.replace("'", "''")
        query = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{project}' "
            "AND [System.State] = 'Closed' "
            f"AND [Microsoft.VSTS.Common.ClosedDate] >= '{start_date.isoformat()}' "
            f"AND [Microsoft.VSTS.Common.ClosedDate] < '{(end_date.fromordinal(end_date.toordinal() + 1)).isoformat()}' "
            "ORDER BY [Microsoft.VSTS.Common.ClosedDate] ASC"
        )
        wiql_response = await self.client.post(
            f"{self.organization_url}/_apis/wit/wiql",
            params={"api-version": "7.1"},
            json={"query": query},
        )
        wiql_response.raise_for_status()
        work_item_ids = [
            item["id"]
            for item in wiql_response.json().get("workItems", [])
            if isinstance(item.get("id"), int)
        ]

        defects: list[dict[str, Any]] = []
        for offset in range(0, len(work_item_ids), 200):
            batch_ids = work_item_ids[offset : offset + 200]
            response = await self.client.get(
                f"{self.organization_url}/_apis/wit/workitems",
                params={
                    "ids": ",".join(str(item_id) for item_id in batch_ids),
                    "fields": ",".join(_FIELDS),
                    "api-version": "7.1",
                },
            )
            response.raise_for_status()
            defects.extend(
                _normalize_work_item(work_item)
                for work_item in response.json().get("value", [])
            )
        return defects


def _normalize_work_item(work_item: dict[str, Any]) -> dict[str, Any]:
    fields = work_item.get("fields", {})
    tags = str(fields.get("System.Tags", "")).split(";")
    created_date = fields.get("System.CreatedDate")
    closed_date = fields.get("Microsoft.VSTS.Common.ClosedDate")
    return {
        "ado_id": int(work_item["id"]),
        "title": str(fields.get("System.Title", "")),
        "description": str(fields.get("System.Description", "")),
        "repro_steps": str(fields.get("Microsoft.VSTS.TCM.ReproSteps", "")),
        "comments": "",
        "module": str(fields.get("System.AreaPath", "")),
        "release": str(fields.get("System.IterationPath", "")),
        "severity": str(fields.get("Microsoft.VSTS.Common.Severity", "")),
        "priority": str(fields.get("Microsoft.VSTS.Common.Priority", "")),
        "state": str(fields.get("System.State", "")),
        "created_date": datetime.fromisoformat(created_date.replace("Z", "+00:00"))
        if created_date
        else None,
        "closed_date": datetime.fromisoformat(closed_date.replace("Z", "+00:00"))
        if closed_date
        else None,
        "tags": [tag.strip() for tag in tags if tag.strip()],
        "source": "ADO_API",
    }
