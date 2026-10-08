"""Move a Hermes state directory (HERMES_HOME) between instances without mixing their SQLite files.

    hermes_state.py export SRC_DIR ARCHIVE    # tar.gz; every SQLite database is an online-backup copy
    hermes_state.py import ARCHIVE DEST_DIR   # verify, extract to staging, move the old content aside, swap in

Hermes keeps state.db (sessions, Kanban, cron, memories), response_store.db and per-profile databases in WAL
mode. Archiving the live files can drop committed transactions that still sit in a -wal file, and extracting
next to another instance's -wal/-shm lets SQLite replay a foreign WAL onto the imported database. So export
never archives -wal/-shm/-journal (each database is a consistent copy made with the backup API, valid even
while Hermes runs), and import never extracts into a directory that still holds the previous instance's
files: they move to DEST_DIR/.pre-import-<ts>-*/ (delete it once the imported state is verified).

Run as root (e.g. through `railway ssh`), both commands switch to the owner of the directory first, so no
root-owned file lands in a volume the hermes user must write. Stdlib only, Python 3.11+ (tarfile filters).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SQLITE_MAGIC = b"SQLite format 3\x00"
SIDECARS = ("-wal", "-shm", "-journal")
# top-level transfer artifacts: the ext4 lost+found, archives in flight, earlier asides and staging dirs
TOP_SKIP = {"lost+found", "hermes-import.tgz", "hermes-export.tgz"}
TOP_SKIP_PREFIXES = (".pre-import-", ".import-staging-")
# the Railway start.sh marker must be earned by setup.py on the importing side; *.aios-bak are setup.py backups
SKIP_NAMES = {".aios-setup-ok"}
SKIP_SUFFIXES = (".aios-bak",)


class StateError(Exception):
    pass


def log(level: str, msg: str, **fields) -> None:
    print(json.dumps({"level": level, "component": "hermes-state", "msg": msg, **fields}), file=sys.stderr,
          flush=True)


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def is_transfer_artifact(rel: Path) -> bool:
    top = rel.parts[0]
    return top in TOP_SKIP or top.startswith(TOP_SKIP_PREFIXES)


def is_sidecar(path: Path) -> bool:
    for suffix in SIDECARS:
        if path.name.endswith(suffix):
            base = path.with_name(path.name[: -len(suffix)])
            return not base.exists() or is_sqlite(base)
    return False


def drop_to_owner(path: Path) -> None:
    if os.geteuid() != 0:
        return
    st = path.stat()
    if st.st_uid == 0:
        return
    os.setgroups([])
    os.setgid(st.st_gid)
    os.setuid(st.st_uid)
    log("info", "running as the directory owner", uid=st.st_uid, gid=st.st_gid)


def sqlite_copy(src: Path, dst: Path) -> None:
    source = sqlite3.connect(src, timeout=30)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
            check = target.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    if check != "ok":
        raise StateError(f"{src}: quick_check of the backup copy failed: {check}")
    shutil.copymode(src, dst)
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))


def export(src: Path, archive: Path) -> None:
    src, archive = src.resolve(), archive.resolve()
    if not src.is_dir():
        raise StateError(f"{src} is not a directory")
    drop_to_owner(src)
    archive.parent.mkdir(parents=True, exist_ok=True)
    tmp = archive.with_name(archive.name + ".tmp")
    databases = files = 0
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-state-") as stage, os.fdopen(fd, "wb") as raw, \
                tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for dirpath, dirnames, filenames in os.walk(src):
                here = Path(dirpath)
                kept = []
                for name in sorted(dirnames):
                    path, rel = here / name, (here / name).relative_to(src)
                    if path.is_symlink():
                        filenames.append(name)  # archived as a link, never followed
                    elif not is_transfer_artifact(rel):
                        tar.add(path, arcname=rel.as_posix(), recursive=False)
                        kept.append(name)
                dirnames[:] = kept
                for name in sorted(filenames):
                    path, rel = here / name, (here / name).relative_to(src)
                    if path in (archive, tmp) or is_transfer_artifact(rel) or name in SKIP_NAMES \
                            or name.endswith(SKIP_SUFFIXES) or (not path.is_symlink() and is_sidecar(path)):
                        continue
                    if path.is_symlink() or (path.is_file() and not is_sqlite(path)):
                        tar.add(path, arcname=rel.as_posix(), recursive=False)
                        files += 1
                    elif path.is_file():
                        copy = Path(stage) / f"{databases}.db"
                        sqlite_copy(path, copy)
                        tar.add(copy, arcname=rel.as_posix(), recursive=False)
                        copy.unlink()
                        databases += 1
                    else:
                        log("warn", "skipping a special file", path=rel.as_posix())
        os.replace(tmp, archive)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log("info", "exported", archive=str(archive), databases=databases, files=files, bytes=archive.stat().st_size)


def checked_members(tar: tarfile.TarFile, dest: Path) -> list[tarfile.TarInfo]:
    members = []
    # getmembers() reads the whole stream, so a truncated or corrupt archive fails before anything changes
    for member in tar.getmembers():
        rel = Path(member.name)
        if not rel.parts or rel.parts == (".",):
            continue
        if is_transfer_artifact(rel) or (len(rel.parts) == 1 and rel.name in SKIP_NAMES):
            log("warn", "archive member skipped", member=member.name)
            continue
        try:
            tarfile.tar_filter(member, str(dest))
        except tarfile.FilterError as exc:
            raise StateError(f"unsafe archive member {member.name!r}: {exc}") from exc
        members.append(member)
    if not members:
        raise StateError("the archive is empty")
    if not any(Path(m.name).parts == ("state.db",) for m in members):
        log("warn", "the archive has no state.db at its root (is it a HERMES_HOME export?)")
    return members


def import_(archive: Path, dest: Path) -> None:
    archive, dest = archive.resolve(), dest.resolve()
    if not archive.is_file():
        raise StateError(f"{archive} not found")
    dest.mkdir(parents=True, exist_ok=True)
    drop_to_owner(dest)
    # moving a directory to another parent needs write access to it (its ".." entry): check before changing anything
    stuck = [e.name for e in dest.iterdir()
             if e.is_dir() and not e.is_symlink() and not is_transfer_artifact(e.relative_to(dest))
             and not os.access(e, os.W_OK)]
    if stuck:
        raise StateError(f"cannot move {sorted(stuck)} aside (not writable by uid {os.geteuid()}): chown them to the "
                         f"owner of {dest} first")
    stage = Path(tempfile.mkdtemp(prefix=".import-staging-", dir=dest))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = checked_members(tar, stage)
            tar.extractall(stage, members=members, filter="tar")
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    aside = Path(tempfile.mkdtemp(prefix=f".pre-import-{stamp}-", dir=dest))
    moved = 0
    for entry in sorted(dest.iterdir()):
        rel = entry.relative_to(dest)
        if entry in (stage, aside, archive) or is_transfer_artifact(rel):
            continue
        os.rename(entry, aside / entry.name)
        moved += 1
    for entry in sorted(stage.iterdir()):
        os.rename(entry, dest / entry.name)
    stage.rmdir()
    if moved:
        log("info", "imported; the previous content was moved aside", archive=str(archive), dest=str(dest),
            aside=str(aside), moved=moved)
    else:
        aside.rmdir()
        log("info", "imported into an empty directory", archive=str(archive), dest=str(dest))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_exp = sub.add_parser("export", help="archive a HERMES_HOME with consistent SQLite copies")
    p_exp.add_argument("src", type=Path)
    p_exp.add_argument("archive", type=Path)
    p_imp = sub.add_parser("import", help="replace a HERMES_HOME's content with an exported archive")
    p_imp.add_argument("archive", type=Path)
    p_imp.add_argument("dest", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "export":
            export(args.src, args.archive)
        else:
            import_(args.archive, args.dest)
    except (StateError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        log("error", f"{args.cmd} failed", error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
