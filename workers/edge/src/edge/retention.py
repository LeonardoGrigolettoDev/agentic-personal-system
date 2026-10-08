"""Media retention (ARCHITECTURE §23): archive kept MEDIA_RETENTION_DAYS, processing/partials are temporary.

Layout under storage://media/:
  input/        uploads waiting for a pipeline (moved to archive/ after success)
  processing/   16 kHz WAVs and whisper chunks; jobs delete their own, stale leftovers are swept here
  archive/      originals after a successful pipeline; deleted after MEDIA_RETENTION_DAYS (0 = keep)
  transcripts/  .txt/.srt/.json/.summary.md - never deleted here
"""

import logging
import os
import time
from pathlib import Path

from edge.storage import LocalStorage

log = logging.getLogger(__name__)


def _sweep(base: Path, cutoff: float, *, only_partials: bool, dry_run: bool, report: dict, key: str) -> None:
    """Delete files older than `cutoff` under `base` (symlinks are removed, never followed), then empty dirs."""
    if not base.is_dir() or base.is_symlink():
        return
    for dirpath, _dirnames, filenames in os.walk(base, topdown=False, followlinks=False):
        directory = Path(dirpath)
        emptied = False
        for name in filenames:
            if only_partials and not name.endswith(".part"):
                continue
            p = directory / name
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            if st.st_mtime >= cutoff:
                continue
            if not dry_run:
                try:
                    p.unlink()
                except OSError as exc:
                    report["errors"] += 1
                    log.warning("retention could not delete", extra={"path": str(p), "error": str(exc)})
                    continue
            emptied = True
            report[key] += 1
            report["bytes_freed"] += st.st_size
        if only_partials or dry_run or directory == base:
            continue
        try:
            # A fresh empty dir may belong to a job that is about to write into it: leave those alone.
            if emptied or directory.stat().st_mtime < cutoff:
                directory.rmdir()  # only succeeds when empty
        except OSError:
            pass


def maintain(
    storage: LocalStorage, *, retention_days: int, stale_hours: float, dry_run: bool = False, now: float | None = None
) -> dict:
    now = time.time() if now is None else now
    media = storage.root / "media"
    report = {
        "archive_deleted": 0,
        "processing_deleted": 0,
        "partials_deleted": 0,
        "bytes_freed": 0,
        "errors": 0,
        "dry_run": dry_run,
        "retention_days": retention_days,
        "stale_hours": stale_hours,
    }
    stale_cutoff = now - stale_hours * 3600
    if retention_days > 0:
        _sweep(
            media / "archive",
            now - retention_days * 86400,
            only_partials=False,
            dry_run=dry_run,
            report=report,
            key="archive_deleted",
        )
    _sweep(
        media / "processing",
        stale_cutoff,
        only_partials=False,
        dry_run=dry_run,
        report=report,
        key="processing_deleted",
    )
    for sub in ("input", "transcripts"):  # aborted uploads / interrupted atomic writes
        _sweep(media / sub, stale_cutoff, only_partials=True, dry_run=dry_run, report=report, key="partials_deleted")
    log.info("retention done", extra=report)
    return report
