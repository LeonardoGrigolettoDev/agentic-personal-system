"""OpenAI-compatible client for LiteLLM (chat completions + embeddings) with the edge virtual key."""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
# OpenAI/LiteLLM say "length" when max_tokens cut the answer; some providers pass through "max_tokens".
TRUNCATED = frozenset({"length", "max_tokens"})


@dataclass(frozen=True)
class Completion:
    content: str
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason in TRUNCATED


class LLMError(Exception):
    """Transport or HTTP failure talking to LiteLLM (as opposed to a bad answer)."""

    def __init__(self, message: str, *, status: int | None = None, timeout: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.timeout = timeout


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout: float,
        *,
        retries: int = 1,
        backoff: float = 1.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(headers=headers, timeout=httpx.Timeout(timeout, connect=10.0))

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.post(url, json=payload)
            except httpx.TimeoutException:
                # A timed-out local model is busy, not flaky: retrying just doubles the wait.
                raise LLMError(f"{payload.get('model')}: timeout", timeout=True) from None
            except httpx.TransportError as exc:
                problem, status = f"{type(exc).__name__}", None
            else:
                if resp.status_code < 400:
                    try:
                        return resp.json()
                    except ValueError:
                        raise LLMError(f"{payload.get('model')}: resposta não é JSON") from None
                problem, status = f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code
                if status not in RETRYABLE_STATUS:
                    raise LLMError(f"{payload.get('model')}: {problem}", status=status)
            if attempt < self.retries:
                log.warning("litellm call failed, retrying", extra={"model": payload.get("model"), "problem": problem})
                time.sleep(self.backoff * 2**attempt)
        raise LLMError(f"{payload.get('model')}: {problem}", status=status)

    def chat(
        self, model: str, messages: list[dict], *, max_tokens: int, temperature: float = 0.2, json_mode: bool = True
    ) -> Completion:
        payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = self._post("/v1/chat/completions", payload)
        try:
            choice = body["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError(f"{model}: resposta sem choices[0].message") from None
        reason = choice.get("finish_reason")
        return Completion(content, reason if isinstance(reason, str) else None)

    def embed(self, model: str, texts: Sequence[str], *, batch_size: int = 16) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            body = self._post("/v1/embeddings", {"model": model, "input": batch})
            data = sorted(body.get("data") or [], key=lambda item: item.get("index", 0))
            if len(data) != len(batch):
                raise LLMError(f"{model}: esperava {len(batch)} embeddings, recebeu {len(data)}")
            vectors.extend(item["embedding"] for item in data)
        return vectors
