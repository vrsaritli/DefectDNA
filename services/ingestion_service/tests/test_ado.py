from datetime import date

import httpx
import pytest

from services.ingestion_service.app.ado import AdoDefectSource


@pytest.mark.asyncio
async def test_ado_source_reads_closed_items_and_normalizes_fields() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/wiql"):
            return httpx.Response(200, json={"workItems": [{"id": 48213}]})
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 48213,
                        "fields": {
                            "System.Title": "Checkout total",
                            "System.AreaPath": "Checkout",
                            "System.IterationPath": "Release 3",
                            "System.State": "Closed",
                            "System.Tags": "regression; billing",
                            "Microsoft.VSTS.Common.ClosedDate": "2026-09-10T00:00:00Z",
                        },
                    }
                ]
            },
        )

    source = AdoDefectSource(
        "https://dev.azure.com/org",
        "Sample's project",
        "test-pat",
        transport=httpx.MockTransport(handler),
    )
    try:
        defects = await source.fetch_closed_defects(date(2026, 9, 1), date(2026, 9, 30))
    finally:
        await source.close()

    assert len(requests) == 2
    assert "Sample''s project" in requests[0].content.decode()
    assert "2026-10-01" in requests[0].content.decode()
    assert defects[0]["ado_id"] == 48213
    assert defects[0]["tags"] == ["regression", "billing"]
    assert defects[0]["closed_date"].isoformat() == "2026-09-10T00:00:00+00:00"


@pytest.mark.asyncio
async def test_ado_source_propagates_provider_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    source = AdoDefectSource(
        "https://dev.azure.com/org",
        "project",
        "invalid",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch_closed_defects(date(2026, 1, 1), date(2026, 1, 2))
    finally:
        await source.close()


def test_ado_source_requires_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        AdoDefectSource("http://dev.azure.com/org", "project", "test-pat")
