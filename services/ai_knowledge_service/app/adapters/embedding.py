"""Embedding adapter interface and the Cohere implementation (v2 embed API).

Errors carry stable codes only (never input text), so they are safe to log and to
return to callers.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

import httpx

InputType = Literal["search_document", "search_query"]
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_TEXTS_PER_CALL = 96  # Cohere embed limit per request
_MAX_BACKOFF_SECONDS = 20.0


class EmbeddingAdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EmbeddingClient(Protocol):
    model: str
    dimensions: int

    async def embed(
        self, texts: list[str], input_type: InputType | None = None
    ) -> list[list[float]]: ...


class CohereEmbeddingAdapter:
    def __init__(
        self,
        api_key: str,
        model: str,
        input_type: str = "search_document",
        timeout_seconds: float = 30.0,
        *,
        dimensions: int = 1024,
        base_url: str = "https://api.cohere.com",
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.input_type = input_type
        self.timeout_seconds = timeout_seconds
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._transport = transport
        self._sleep = sleep

    async def embed(
        self, texts: list[str], input_type: InputType | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise EmbeddingAdapterError("COHERE_NOT_CONFIGURED")
        kind = input_type or self.input_type
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self.api_key}"},
        ) as client:
            for start in range(0, len(texts), _MAX_TEXTS_PER_CALL):
                chunk = texts[start : start + _MAX_TEXTS_PER_CALL]
                vectors.extend(await self._embed_chunk(client, chunk, kind))
        return vectors

    async def _embed_chunk(
        self, client: httpx.AsyncClient, texts: list[str], input_type: str
    ) -> list[list[float]]:
        body = {
            "model": self.model,
            "texts": texts,
            "input_type": input_type,
            "embedding_types": ["float"],
            "truncate": "END",
        }
        attempt = 0
        while True:
            retry_after: float | None = None
            error: EmbeddingAdapterError
            cause: Exception
            try:
                response = await client.post("/v2/embed", json=body)
            except httpx.TimeoutException as exc:
                error, cause = EmbeddingAdapterError("EMBEDDING_TIMEOUT"), exc
            except httpx.RequestError as exc:
                error, cause = EmbeddingAdapterError("EMBEDDING_UNAVAILABLE"), exc
            else:
                if response.status_code in {401, 403}:
                    raise EmbeddingAdapterError("EMBEDDING_AUTH_FAILED")
                if response.is_success:
                    return self._parse(response, len(texts))
                if response.status_code not in _RETRYABLE_STATUS:
                    raise EmbeddingAdapterError(f"EMBEDDING_HTTP_{response.status_code}")
                error = EmbeddingAdapterError(
                    "EMBEDDING_RATE_LIMITED"
                    if response.status_code == 429
                    else f"EMBEDDING_HTTP_{response.status_code}"
                )
                cause = error
                header = response.headers.get("retry-after")
                retry_after = float(header) if header and header.replace(".", "", 1).isdigit() else None
            if attempt >= self.max_retries:
                raise error from cause
            await self._sleep(min(retry_after or float(2**attempt), _MAX_BACKOFF_SECONDS))
            attempt += 1

    def _parse(self, response: httpx.Response, expected: int) -> list[list[float]]:
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise EmbeddingAdapterError("EMBEDDING_OUTPUT_INVALID") from exc
        embeddings = (payload.get("embeddings") or {}).get("float")
        if not isinstance(embeddings, list) or len(embeddings) != expected:
            raise EmbeddingAdapterError("EMBEDDING_OUTPUT_INVALID")
        vectors = [list(map(float, item)) for item in embeddings]
        if any(len(vector) != self.dimensions for vector in vectors):
            raise EmbeddingAdapterError("EMBEDDING_DIMENSION_MISMATCH")
        return vectors


def build_embedding_client(settings: Any) -> CohereEmbeddingAdapter | None:
    """Cohere client from settings, or None when no key is configured (RAG disabled)."""
    if not settings.cohere_api_key:
        return None
    return CohereEmbeddingAdapter(
        settings.cohere_api_key,
        settings.cohere_embed_model,
        settings.cohere_embed_input_type,
        settings.cohere_timeout_seconds,
        dimensions=settings.cohere_embed_dimensions,
        base_url=settings.cohere_base_url,
        max_retries=settings.cohere_max_retries,
    )
