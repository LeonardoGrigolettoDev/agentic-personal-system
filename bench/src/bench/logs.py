"""JSON (default) or text logs on stderr; stdout stays for results, reports and diffs."""

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
               "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        out.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = " ".join(f"{k}={v}" for k, v in vars(record).items() if k not in _STANDARD)
        return f"{record.levelname.lower():7} {record.getMessage()}" + (f"  {extra}" if extra else "")


def setup(fmt: str = "json", verbose: int = 0) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose > 1 else logging.INFO)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING if verbose < 2 else logging.DEBUG)
