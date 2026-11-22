import json

import httpx
import pytest

from services.ai_knowledge_service.app.adapters.llm import (
    GroqLLMClient,
    LLMOutputError,
    LLMProviderError,
)

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _ok(content: str = '{"results": []}', finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


async def _no_sleep(_: float) -> None:
    return None


def _client(handler: httpx.MockTransport, retries: int = 2) -> GroqLLMClient:
    return GroqLLMClient(
        "test-key",
        "openai/gpt-oss-120b",
        transport=handler,
        max_retries=retries,
        sleep=_no_sleep,
    )


async def test_request_uses_strict_json_schema_and_token_cap() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return _ok()

    client = _client(httpx.MockTransport(handler))
    response = await client.complete_json(
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        schema_name="defect_rca_batch",
        max_completion_tokens=1234,
    )
    await client.close()

    body = seen["body"]
    assert isinstance(body, dict)
    assert seen["path"] == "/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_completion_tokens"] == 1234
    assert body["temperature"] == 0
    assert response.usage["total_tokens"] == 15


async def test_rate_limit_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {}})
        return _ok()

    client = _client(httpx.MockTransport(handler))
    await client.complete_json(messages=[], schema=SCHEMA, schema_name="x")
    await client.close()
    assert calls["n"] == 2


async def test_auth_failure_is_not_retried_and_hides_body() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "secret detail"}})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LLMProviderError) as error:
        await client.complete_json(messages=[], schema=SCHEMA, schema_name="x")
    await client.close()
    assert calls["n"] == 1
    assert error.value.code == "LLM_AUTH_FAILED"
    assert "secret detail" not in str(error.value)


async def test_server_errors_exhaust_retries() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(503, json={})), retries=2)
    with pytest.raises(LLMProviderError) as error:
        await client.complete_json(messages=[], schema=SCHEMA, schema_name="x")
    await client.close()
    assert error.value.code == "LLM_PROVIDER_ERROR"


async def test_truncated_and_schema_rejected_outputs_are_output_errors() -> None:
    truncated = _client(httpx.MockTransport(lambda _: _ok(finish_reason="length")))
    with pytest.raises(LLMOutputError) as error:
        await truncated.complete_json(messages=[], schema=SCHEMA, schema_name="x")
    await truncated.close()
    assert error.value.code == "LLM_OUTPUT_TRUNCATED"

    rejected = _client(
        httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": {"code": "json_validate_failed"}})
        )
    )
    with pytest.raises(LLMOutputError) as error:
        await rejected.complete_json(messages=[], schema=SCHEMA, schema_name="x")
    await rejected.close()
    assert error.value.code == "LLM_SCHEMA_REJECTED"
