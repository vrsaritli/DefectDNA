import httpx
import pytest

from services.ingestion_service.app.ado import (
    AdoDefectSource,
    AdoInputError,
    organization_url,
    parse_work_item_ids,
    query_id_from_link,
)

GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def test_pat_only_goes_to_azure_devops_hosts() -> None:
    assert organization_url("rathna070") == "https://dev.azure.com/rathna070"
    assert organization_url("https://dev.azure.com/acme/Some Project") == "https://dev.azure.com/acme"
    assert organization_url("https://acme.visualstudio.com/") == "https://acme.visualstudio.com"
    for bad in ("https://evil.example/acme", "http://dev.azure.com/acme", "https://user:pw@dev.azure.com/x"):
        with pytest.raises(AdoInputError):
            organization_url(bad)


def test_query_link_and_id_parsing() -> None:
    assert query_id_from_link(f"https://dev.azure.com/o/p/_queries/query/{GUID.upper()}/") == GUID
    assert query_id_from_link(f"https://o.visualstudio.com/p/_queries?qid={GUID}") == GUID
    with pytest.raises(AdoInputError):
        query_id_from_link("https://dev.azure.com/o/p/_queries/query/not-a-guid")
    assert parse_work_item_ids("303, #419 419\n450") == [303, 419, 450]
    with pytest.raises(AdoInputError):
        parse_work_item_ids("303, abc")


async def test_saved_tree_query_with_comments() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        if "/_apis/wit/wiql/" in request.url.path:
            return httpx.Response(
                200,
                json={"workItemRelations": [{"target": {"id": 7}}, {"target": {"id": 8}}, {"target": {"id": 7}}]},
            )
        if request.url.path.endswith("/comments"):
            return httpx.Response(
                200, json={"comments": [{"text": "<p>Root cause: binding</p>", "createdBy": {"displayName": "Dev"}}]}
            )
        return httpx.Response(
            200,
            json={"value": [{"id": 7, "fields": {"System.Title": "A"}}, {"id": 8, "fields": {"System.Title": "B"}}]},
        )

    source = AdoDefectSource(
        "https://dev.azure.com/acme", "Sample demo", "pat", transport=httpx.MockTransport(handler)
    )
    defects = await source.fetch_saved_query(GUID, include_comments=True)
    await source.close()

    assert [d["ado_id"] for d in defects] == [7, 8]
    assert defects[0]["comments"][0]["author"] == "Dev"
    assert any(path.startswith("/acme/Sample%20demo/_apis/wit/wiql/") for path in seen)
