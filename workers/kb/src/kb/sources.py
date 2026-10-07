"""File discovery, abstract source URIs and the Nitro/Pessoal tenant isolation guard."""

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

TENANTS = ("nitro", "pessoal", "shared")
DOMAINS = ("chief", "engineering", "finance", "projects", "personal", "learning")
SOURCE_TYPES = ("obsidian", "file", "url", "audio", "video", "manual", "agent")

IGNORED_DIRS = frozenset({".obsidian", ".trash", "Templates"})
# Vaults that belong to exactly one tenant (compared case-insensitively).
PROTECTED_VAULTS = {"nitro": "nitro", "pessoal": "pessoal"}


class TenantGuardError(Exception):
    pass


@dataclass(frozen=True)
class SourceRef:
    path: Path
    uri: str
    source_type: str
    vault: str | None


@dataclass
class Discovery:
    files: list[Path] = field(default_factory=list)
    vault_roots: set[Path] = field(default_factory=set)  # vaults found strictly below the ingest root


def find_vault_root(path: Path) -> Path | None:
    """Nearest ancestor (or the dir itself) containing a `.obsidian` folder."""
    path = path.resolve()
    start = path if path.is_dir() else path.parent
    for candidate in [start, *start.parents]:
        if (candidate / ".obsidian").is_dir():
            return candidate
    return None


def _ignored(name: str) -> bool:
    return name.startswith(".") or name in IGNORED_DIRS


def discover(root: Path) -> Discovery:
    """All non-hidden files under `root` (sorted, deterministic), skipping Obsidian internals/templates."""
    root = root.resolve()
    found = Discovery()
    if root.is_file():
        found.files.append(root)
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        if ".obsidian" in dirnames and directory != root:
            found.vault_roots.add(directory)
        dirnames[:] = sorted(d for d in dirnames if not _ignored(d))
        found.files.extend(directory / f for f in sorted(filenames) if not f.startswith("."))
    return found


def root_label(root: Path, label: str | None) -> str:
    root = root.resolve()
    base = root if root.is_dir() else root.parent
    return (label or base.name or "root").strip("/") or "root"


def resolve_source(path: Path, root: Path, label: str | None = None) -> SourceRef:
    """Abstract URI for `path`: obsidian://<Vault>/<rel> inside a vault, else file://<label>/<rel>."""
    path = path.resolve()
    vault = find_vault_root(path)
    if vault is not None:
        rel = path.relative_to(vault).as_posix()
        return SourceRef(path, f"obsidian://{vault.name}/{rel}", "obsidian", vault.name)
    root = root.resolve()
    base = root if root.is_dir() else root.parent
    rel = path.relative_to(base).as_posix()
    return SourceRef(path, f"file://{root_label(root, label)}/{rel}", "file", None)


def check_tenant(vault: str | None, tenant: str) -> None:
    """Refuse to mix the work and personal vaults into the wrong tenant."""
    if tenant not in TENANTS:
        raise TenantGuardError(f"unknown tenant {tenant!r}")
    if vault is None:
        return
    required = PROTECTED_VAULTS.get(vault.casefold())
    if required and tenant != required:
        raise TenantGuardError(
            f"vault {vault!r} belongs to tenant {required!r}; refusing to ingest it as {tenant!r}"
        )


def guard_root(root: Path, tenant: str, discovery: Discovery) -> None:
    """Check the root itself plus every vault found below it, before anything is read or written."""
    for vault in [find_vault_root(root), *sorted(discovery.vault_roots)]:
        check_tenant(vault.name if vault else None, tenant)


def prune_prefixes(root: Path, label: str | None, discovery: Discovery) -> list[str]:
    """URI prefixes fully covered by this ingest root (documents under them may be pruned)."""
    root = root.resolve()
    vault = find_vault_root(root)
    if vault is not None:
        rel = root.relative_to(vault).as_posix()
        return [f"obsidian://{vault.name}/" + ("" if rel == "." else f"{rel}/")]
    return [f"file://{root_label(root, label)}/"] + [f"obsidian://{v.name}/" for v in sorted(discovery.vault_roots)]


def iter_sources(root: Path, label: str | None, discovery: Discovery) -> Iterator[SourceRef]:
    for path in discovery.files:
        yield resolve_source(path, root, label)
