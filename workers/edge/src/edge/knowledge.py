"""Client for the knowledge service (workers/kb): POST /v1/ingest stores a document for one tenant."""

import httpx

from edge.errors import Unavailable, Unprocessable, UpstreamError, UpstreamTimeout


class KnowledgeClient:
    def __init__(
        self, base_url: str, api_key: str | None, timeout: float, *, client: httpx.Client | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(headers=headers, timeout=httpx.Timeout(timeout, connect=10.0))

    def close(self) -> None:
        self._client.close()

    def ingest(
        self,
        *,
        tenant: str,
        source_uri: str,
        title: str,
        content: str,
        source_type: str,
        domain: str | None,
        metadata: dict,
    ) -> dict:
        payload = {
            "tenant": tenant,
            "source_uri": source_uri,
            "title": title,
            "content": content,
            "source_type": source_type,
            "domain": domain,
            "metadata": metadata,
        }
        try:
            resp = self._client.post(f"{self.base_url}/v1/ingest", json=payload)
        except httpx.TimeoutException:
            raise UpstreamTimeout(f"knowledge não respondeu em {self.timeout:.0f}s") from None
        except httpx.TransportError as exc:
            raise Unavailable(f"knowledge indisponível em {self.base_url}: {type(exc).__name__}") from None
        if resp.status_code in (400, 403, 422):
            raise Unprocessable(f"knowledge recusou o documento: {_detail(resp)}")
        if resp.status_code >= 400:
            raise UpstreamError(f"knowledge respondeu HTTP {resp.status_code}: {_detail(resp)}")
        return resp.json()


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body)[:300]
    return str(body)[:300]
