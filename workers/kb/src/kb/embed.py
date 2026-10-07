"""OpenAI-compatible embeddings client for LiteLLM (alias embed-local -> qwen3-embedding:0.6b)."""

import logging
import time
from collections.abc import Sequence

import httpx

from kb.config import Settings

log = logging.getLogger(__name__)

# Qwen3-Embedding is asymmetric: queries carry an instruction, documents are embedded as-is.
QUERY_INSTRUCTION = "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class EmbeddingError(Exception):
    pass


def query_text(query: str) -> str:
    return QUERY_INSTRUCTION + query


class Embedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        batch_size: int = 16,
        timeout: float = 300.0,
        max_retries: int = 4,
        backoff: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model = model
        self.model_name = model_name or model
        self.dim = dim
        self.batch_size = max(1, batch_size)
        self.max_retries = max_retries
        self.backoff = backoff
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> "Embedder":
        return cls(
            settings.litellm_base_url,
            settings.embed_model,
            settings.embed_dim,
            api_key=settings.litellm_key,
            model_name=settings.embed_model_name,
            batch_size=settings.embed_batch_size,
            timeout=settings.embed_timeout,
            **kwargs,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Embedder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(list(texts[start : start + self.batch_size])))
        return vectors

    def embed_query(self, query: str) -> list[float]:
        return self._embed_batch([query_text(query)])[0]

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch}
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post("/v1/embeddings", json=payload)
                if response.status_code not in RETRYABLE_STATUS:
                    response.raise_for_status()
                    return self._parse(response.json(), len(batch))
                problem = f"HTTP {response.status_code}"
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}: {exc}"
            except httpx.HTTPStatusError as exc:
                status, body = exc.response.status_code, exc.response.text[:300]
                raise EmbeddingError(f"embeddings request failed: HTTP {status} {body}") from exc
            if attempt == self.max_retries:
                raise EmbeddingError(f"embeddings request failed after {attempt + 1} attempts: {problem}")
            delay = self.backoff * 2**attempt
            log.warning("embeddings %s; retrying in %.1fs", problem, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def _parse(self, body: dict, expected: int) -> list[list[float]]:
        data = sorted(body.get("data") or [], key=lambda item: item.get("index", 0))
        if len(data) != expected:
            raise EmbeddingError(f"expected {expected} embeddings, got {len(data)}")
        vectors = [item["embedding"] for item in data]
        for vector in vectors:
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"embedding has {len(vector)} dims, expected {self.dim} (KB_EMBED_DIM / vector({self.dim}))"
                )
        return vectors
