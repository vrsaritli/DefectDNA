"""Read-only Azure DevOps source: connection test, saved queries, work item IDs, closed
defects by date, and full comment history.

The PAT always comes from server configuration (``ADO_PAT``) and is only ever sent to
Azure DevOps hosts (``dev.azure.com`` or ``*.visualstudio.com``). A saved-query link from
a user is never fetched as a URL: only its query GUID is extracted and looked up in the
configured organization.
"""

import asyncio
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlparse

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
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ADO_HOSTS = ("dev.azure.com",)
_ADO_HOST_SUFFIX = ".visualstudio.com"
_MAX_ITEMS = 5_000
_COMMENT_CONCURRENCY = 8


class AdoInputError(ValueError):
    """User input (query link, IDs, organization) that cannot be used; carries a code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def organization_url(value: str) -> str:
    """Accept an organization name or URL; return its HTTPS URL on an ADO host only."""
    value = value.strip().rstrip("/")
    if not value:
        raise AdoInputError("ADO_ORGANIZATION_REQUIRED", "Organization is required")
    if "://" not in value:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
            raise AdoInputError("ADO_ORGANIZATION_INVALID", "Organization name is not valid")
        return f"https://dev.azure.com/{value}"
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not (host in _ADO_HOSTS or host.endswith(_ADO_HOST_SUFFIX))
    ):
        raise AdoInputError(
            "ADO_ORGANIZATION_INVALID",
            "Organization must be an https://dev.azure.com/<org> or *.visualstudio.com URL",
        )
    if host in _ADO_HOSTS:
        org = parsed.path.strip("/").split("/")[0]
        if not org:
            raise AdoInputError("ADO_ORGANIZATION_INVALID", "Organization name is missing")
        return f"https://dev.azure.com/{org}"
    return f"https://{host}"


def query_id_from_link(link: str) -> str:
    """Saved-query GUID from an ADO query link (…/_queries/query/<guid>, ?qid=<guid>) or a bare GUID."""
    match = _GUID.search(link or "")
    if not match:
        raise AdoInputError(
            "ADO_QUERY_LINK_INVALID",
            "Paste a saved query link such as https://dev.azure.com/<org>/<project>/_queries/query/<id>",
        )
    return match.group(0).lower()


def parse_work_item_ids(text: str) -> list[int]:
    ids: list[int] = []
    for token in re.split(r"[\s,;]+", text or ""):
        if not token:
            continue
        token = token.lstrip("#")
        if not token.isdigit() or int(token) < 1:
            raise AdoInputError("ADO_IDS_INVALID", f"'{token[:20]}' is not a work item ID")
        ids.append(int(token))
    if not ids:
        raise AdoInputError("ADO_IDS_INVALID", "Enter at least one work item ID")
    return list(dict.fromkeys(ids))


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
        self._project_path = quote(project, safe="")
        self.client = httpx.AsyncClient(
            auth=httpx.BasicAuth("", pat),
            timeout=httpx.Timeout(20.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def check_connection(self) -> dict[str, Any]:
        """Prove the PAT can read the project (one cheap call)."""
        response = await self.client.get(
            f"{self.organization_url}/_apis/projects/{self._project_path}",
            params={"api-version": "7.1"},
        )
        response.raise_for_status()
        payload = response.json()
        return {"project_id": payload.get("id"), "project_name": payload.get("name")}

    async def find_field(self, name: str) -> str | None:
        """Reference name (e.g. ``Custom.RootCauseRCA``) for a field's display or reference name."""
        response = await self.client.get(
            f"{self.organization_url}/{self._project_path}/_apis/wit/fields",
            params={"api-version": "7.1"},
        )
        response.raise_for_status()
        wanted = name.strip().casefold()
        for field in response.json().get("value", []):
            if wanted in {
                str(field.get("name", "")).casefold(),
                str(field.get("referenceName", "")).casefold(),
            }:
                return str(field["referenceName"])
        return None

    async def fetch_closed_defects(
        self,
        start_date: date,
        end_date: date,
        *,
        include_comments: bool = False,
        extra_fields: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        project = self.project.replace("'", "''")
        end_exclusive = date.fromordinal(end_date.toordinal() + 1)
        query = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{project}' "
            "AND [System.State] = 'Closed' "
            f"AND [Microsoft.VSTS.Common.ClosedDate] >= '{start_date.isoformat()}' "
            f"AND [Microsoft.VSTS.Common.ClosedDate] < '{end_exclusive.isoformat()}' "
            "ORDER BY [Microsoft.VSTS.Common.ClosedDate] ASC"
        )
        response = await self.client.post(
            f"{self.organization_url}/_apis/wit/wiql",
            params={"api-version": "7.1"},
            json={"query": query},
        )
        response.raise_for_status()
        return await self.fetch_by_ids(
            _ids_from_wiql(response.json()),
            include_comments=include_comments,
            extra_fields=extra_fields,
        )

    async def fetch_saved_query(
        self,
        query_id: str,
        *,
        include_comments: bool = False,
        extra_fields: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Run a saved query (flat, tree or direct links) and fetch every result item."""
        response = await self.client.get(
            f"{self.organization_url}/{self._project_path}/_apis/wit/wiql/{query_id}",
            params={"api-version": "7.1"},
        )
        response.raise_for_status()
        return await self.fetch_by_ids(
            _ids_from_wiql(response.json()),
            include_comments=include_comments,
            extra_fields=extra_fields,
        )

    async def fetch_by_ids(
        self,
        work_item_ids: list[int],
        *,
        include_comments: bool = False,
        extra_fields: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Work items as defects; values of ``extra_fields`` are returned under ``"_extra"``."""
        if len(work_item_ids) > _MAX_ITEMS:
            raise AdoInputError("ADO_TOO_MANY_ITEMS", f"More than {_MAX_ITEMS} work items")
        defects: list[dict[str, Any]] = []
        for offset in range(0, len(work_item_ids), 200):
            batch_ids = work_item_ids[offset : offset + 200]
            response = await self.client.get(
                f"{self.organization_url}/_apis/wit/workitems",
                params={
                    "ids": ",".join(str(item_id) for item_id in batch_ids),
                    "fields": ",".join((*_FIELDS, *extra_fields)),
                    "errorPolicy": "omit",
                    "api-version": "7.1",
                },
            )
            response.raise_for_status()
            for work_item in response.json().get("value", []):
                if not isinstance(work_item, dict) or not work_item.get("id"):
                    continue
                defect = _normalize_work_item(work_item)
                if extra_fields:
                    fields = work_item.get("fields", {})
                    defect["_extra"] = {name: fields.get(name) for name in extra_fields}
                defects.append(defect)
        if include_comments and defects:
            limiter = asyncio.Semaphore(_COMMENT_CONCURRENCY)

            async def attach(defect: dict[str, Any]) -> None:
                async with limiter:
                    defect["comments"] = await self.fetch_comments(int(defect["ado_id"]))

            await asyncio.gather(*(attach(defect) for defect in defects))
        return defects

    async def fetch_comments(self, work_item_id: int) -> list[dict[str, Any]]:
        """Every comment on a work item, oldest first (follows continuation tokens)."""
        comments: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"api-version": "7.1-preview.4", "$top": 200, "order": "asc"}
            if token:
                params["continuationToken"] = token
            response = await self.client.get(
                f"{self.organization_url}/{self._project_path}/_apis/wit/workItems/"
                f"{work_item_id}/comments",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
            for comment in payload.get("comments", []):
                created = comment.get("createdDate")
                comments.append(
                    {
                        "author": str((comment.get("createdBy") or {}).get("displayName", "")),
                        "text": str(comment.get("text", "")),
                        "at": datetime.fromisoformat(created) if created else None,
                    }
                )
            token = payload.get("continuationToken")
            if not token:
                return comments


def _ids_from_wiql(payload: dict[str, Any]) -> list[int]:
    """IDs from a WIQL result: flat ``workItems`` or tree/link ``workItemRelations``."""
    ids: list[int] = []
    for item in payload.get("workItems") or []:
        if isinstance(item.get("id"), int):
            ids.append(item["id"])
    for relation in payload.get("workItemRelations") or []:
        target = relation.get("target") or {}
        if isinstance(target.get("id"), int):
            ids.append(target["id"])
    return list(dict.fromkeys(ids))


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
        "created_date": datetime.fromisoformat(created_date) if created_date else None,
        "closed_date": datetime.fromisoformat(closed_date) if closed_date else None,
        "tags": [tag.strip() for tag in tags if tag.strip()],
        "source": "ADO_API",
    }
