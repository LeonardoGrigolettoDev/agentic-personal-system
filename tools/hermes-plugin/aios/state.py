"""Per-session plugin state, bounded (LRU) because the gateway is long-lived."""

import threading
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class SessionState:
    routed: bool = False
    run: dict | None = None
    tenant: str | None = None
    model: str | None = None
    budget_state: str = "ok"
    context_injected: bool = False
    skills: set[str] = field(default_factory=set)
    last_test: dict | None = None
    outcome: str | None = None  # succeeded | failed | blocked


class Sessions:
    def __init__(self, max_sessions: int = 512) -> None:
        self._max = max(16, max_sessions)
        self._items: OrderedDict[str, SessionState] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            s = self._items.get(session_id)
            if s is None:
                s = self._items[session_id] = SessionState()
                while len(self._items) > self._max:
                    self._items.popitem(last=False)
            else:
                self._items.move_to_end(session_id)
            return s

    def peek(self, session_id: str) -> SessionState | None:
        with self._lock:
            return self._items.get(session_id)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._items.pop(session_id, None)
