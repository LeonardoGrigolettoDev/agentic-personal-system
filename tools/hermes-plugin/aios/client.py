"""Tiny JSON-over-HTTP client for the Decision and Knowledge services (stdlib only: runs inside Hermes)."""

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("aios.client")


class ServiceError(Exception):
    pass


@dataclass
class Service:
    name: str
    base_url: str
    api_key: str
    timeout: float

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    def post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        return self._call("POST", path, body, timeout)

    def get(self, path: str, timeout: float | None = None) -> dict:
        return self._call("GET", path, None, timeout)

    def _call(self, method: str, path: str, body: dict | None, timeout: float | None) -> dict:
        if not self.enabled:
            raise ServiceError(f"{self.name} not configured")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url.rstrip("/") + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            raise ServiceError(f"{self.name} {method} {path}: HTTP {exc.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ServiceError(f"{self.name} {method} {path}: {exc}") from None


def decision() -> Service:
    return Service("decision", os.environ.get("AIOS_DECISION_URL", "http://decision:8080"),
                   os.environ.get("DECISION_API_KEY", ""), float(os.environ.get("AIOS_DECISION_TIMEOUT", "5")))


def knowledge() -> Service:
    return Service("knowledge", os.environ.get("AIOS_KNOWLEDGE_URL", "http://knowledge:8080"),
                   os.environ.get("KNOWLEDGE_API_KEY", ""), float(os.environ.get("AIOS_KNOWLEDGE_TIMEOUT", "20")))
