from typing import Any

import httpx


class EmbeddingAdapterError(RuntimeError):
    pass


class CohereEmbeddingAdapter:
    def __init__(self, api_key: str, model: str, input_type: str = "search_document", timeout_seconds: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.input_type = input_type
        self.timeout_seconds = timeout_seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise EmbeddingAdapterError("COHERE_NOT_CONFIGURED")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    "https://api.cohere.ai/v2/embed",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={"model": self.model, "texts": texts, "input_type": self.input_type, "embedding_types": ["float"]},
                )
        except httpx.TimeoutException as exc:
            raise EmbeddingAdapterError("EMBEDDING_TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise EmbeddingAdapterError("EMBEDDING_UNAVAILABLE") from exc

        if response.status_code in {401, 403}:
            raise EmbeddingAdapterError("EMBEDDING_AUTH_FAILED")
        if response.status_code >= 400:
            raise EmbeddingAdapterError(f"EMBEDDING_HTTP_{response.status_code}")
        payload: dict[str, Any] = response.json()
        embeddings = (payload.get("embeddings") or {}).get("float")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingAdapterError("EMBEDDING_OUTPUT_INVALID")
        return [list(map(float, item)) for item in embeddings]
