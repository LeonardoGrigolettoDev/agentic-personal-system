"""`edge serve` (uvicorn, single process) and `edge maintain` (media retention, prints a JSON report)."""

import argparse
import json
import sys

from edge.config import ConfigError, Settings
from edge.logs import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edge", description="AIOS agent-edge media worker")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    maint = sub.add_parser("maintain", help="delete expired archive media and stale processing files")
    maint.add_argument("--dry-run", action="store_true", help="report what would be deleted")
    maint.add_argument("--retention-days", type=int, help="override MEDIA_RETENTION_DAYS")
    maint.add_argument("--stale-hours", type=float, help="override EDGE_PROCESSING_STALE_HOURS")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"edge: {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "edge.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=20,
        )
        return 0

    from edge import retention
    from edge.storage import LocalStorage

    setup_logging(settings.log_level, stream=sys.stderr)  # stdout carries only the JSON report
    report = retention.maintain(
        LocalStorage(settings.storage_root),
        retention_days=settings.retention_days if args.retention_days is None else args.retention_days,
        stale_hours=settings.processing_stale_hours if args.stale_hours is None else args.stale_hours,
        dry_run=args.dry_run,
    )
    print(json.dumps(report))
    return 0 if report["errors"] == 0 else 1
