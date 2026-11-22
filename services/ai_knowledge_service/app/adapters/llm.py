"""LLM adapter interface and the Groq implementation (OpenAI-compatible chat API).

Error messages raised from here carry only stable codes, never prompt or response
text, so they are safe to log and to store in job errors.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 30.0


class LLMError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LLMProviderError(LLMError):
    """The provider could not be reached or refused the request."""


class LLMOutputError(LLMError):
    """The provider answered, but the answer is unusable (truncated, not schema-valid)."""


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    model: str

    async def complete_json(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        schema_name: str,
        max_completion_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def close(self) -> None: ...


class GroqLLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        reasoning_effort: str | None = "medium",
        max_completion_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is required")
        self.model = model
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def complete_json(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        schema_name: str,
        max_completion_tokens: int | None = None,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": max_completion_tokens or self.max_completion_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        attempt = 0
        while True:
            retry_after: float | None = None
            try:
                response = await self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                error: LLMProviderError = LLMProviderError("LLM_TIMEOUT")
                cause: Exception = exc
            except httpx.TransportError as exc:
                error = LLMProviderError("LLM_UNAVAILABLE")
                cause = exc
            else:
                if response.is_success:
                    return self._parse(response)
                if response.status_code in (401, 403):
                    raise LLMProviderError("LLM_AUTH_FAILED")
                if response.status_code == 400 and _error_code(response) == "json_validate_failed":
                    raise LLMOutputError("LLM_SCHEMA_REJECTED")
                if response.status_code not in _RETRYABLE_STATUS:
                    raise LLMProviderError("LLM_REQUEST_REJECTED")
                error = LLMProviderError(
                    "LLM_RATE_LIMITED" if response.status_code == 429 else "LLM_PROVIDER_ERROR"
                )
                cause = error
                retry_after = _retry_after_seconds(response)

            if attempt >= self.max_retries:
                raise error from cause
            delay = retry_after if retry_after is not None else float(2**attempt)
            await self._sleep(min(delay, _MAX_BACKOFF_SECONDS))
            attempt += 1

    def _parse(self, response: httpx.Response) -> LLMResponse:
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMOutputError("LLM_MALFORMED_RESPONSE") from exc
        if choice.get("finish_reason") == "length":
            raise LLMOutputError("LLM_OUTPUT_TRUNCATED")
        if not isinstance(content, str) or not content.strip():
            raise LLMOutputError("LLM_EMPTY_OUTPUT")
        usage = payload.get("usage") or {}
        return LLMResponse(
            content=content,
            model=str(payload.get("model") or self.model),
            usage={
                key: int(value)
                for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(value, int | float)
            },
        )


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
