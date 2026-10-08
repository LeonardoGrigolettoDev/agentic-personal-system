"""Turn report suggestions into a unified diff for config/routing.yaml. The diff is printed, never applied.

Only `task_types.<type>.tier_by_complexity.<level>` values change, and only for the complexity levels the
suggested model was actually benchmarked at. Edits are line based so comments and layout survive; the
result is re-parsed to prove it says exactly what was intended.
"""

import difflib
import re
from dataclasses import dataclass

import yaml

from bench.report import Suggestion

_KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_-]+):\s*(#.*)?$")
_FLOW = re.compile(r"^(\s*tier_by_complexity:\s*)\{(.*)\}(\s*(?:#.*)?)$")
_BLOCK_START = re.compile(r"^(\s*)tier_by_complexity:\s*(#.*)?$")
_BLOCK_ITEM = re.compile(r"^(\s*)([A-Za-z0-9_-]+):(\s*)([^\s#]+)(\s*#.*)?$")


class PatchError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    task_type: str
    complexity: str
    old: str | None
    new: str


def plan_changes(suggestions: list[Suggestion], routing: dict) -> list[Change]:
    task_types = routing.get("task_types") or {}
    changes = []
    for s in suggestions:
        tbc = (task_types.get(s.task_type) or {}).get("tier_by_complexity")
        if not isinstance(tbc, dict):
            continue
        for level in s.complexities:
            if level in tbc and tbc[level] != s.model:
                changes.append(Change(s.task_type, level, tbc[level], s.model))
    return changes


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _block_end(lines: list[str], start: int, indent: int) -> int:
    """First line after `start` whose indent is <= `indent` (end of that mapping's children)."""
    for i in range(start + 1, len(lines)):
        if _content(lines[i]) and _indent(lines[i]) <= indent:
            return i
    return len(lines)


def _find_key(lines: list[str], lo: int, hi: int, key: str, indent: int | None) -> int | None:
    for i in range(lo, hi):
        m = _KEY_LINE.match(lines[i].rstrip("\n"))
        if m and m.group(2) == key and (indent is None or len(m.group(1)) == indent):
            return i
    return None


def _child_indent(lines: list[str], start: int) -> int | None:
    for i in range(start + 1, len(lines)):
        if _content(lines[i]):
            return _indent(lines[i]) if _indent(lines[i]) > _indent(lines[start]) else None
    return None


def _edit_flow(line: str, updates: dict[str, str]) -> str:
    m = _FLOW.match(line.rstrip("\n"))
    mapping = yaml.safe_load("{" + m.group(2) + "}") or {}
    mapping.update(updates)
    body = ", ".join(f"{k}: {v}" for k, v in mapping.items())
    return f"{m.group(1)}{{ {body} }}{m.group(3)}\n"


def _edit_block(lines: list[str], start: int, updates: dict[str, str]) -> None:
    end = _block_end(lines, start, _indent(lines[start]))
    pending = dict(updates)
    item_indent = None
    for i in range(start + 1, end):
        m = _BLOCK_ITEM.match(lines[i].rstrip("\n"))
        if not m:
            continue
        item_indent = m.group(1)
        if m.group(2) in pending:
            lines[i] = f"{m.group(1)}{m.group(2)}:{m.group(3) or ' '}{pending.pop(m.group(2))}{m.group(5) or ''}\n"
    if pending:
        pad = item_indent or " " * (_indent(lines[start]) + 2)
        lines[end:end] = [f"{pad}{k}: {v}\n" for k, v in pending.items()]


def apply_changes(text: str, changes: list[Change]) -> str:
    if not changes:
        return text
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    root = _find_key(lines, 0, len(lines), "task_types", 0)
    if root is None:
        raise PatchError("routing.yaml has no top-level task_types")
    by_type: dict[str, dict[str, str]] = {}
    for c in changes:
        by_type.setdefault(c.task_type, {})[c.complexity] = c.new
    for task_type, updates in by_type.items():
        root_end = _block_end(lines, root, 0)
        child = _child_indent(lines, root)
        tt = _find_key(lines, root + 1, root_end, task_type, child)
        if tt is None:
            raise PatchError(f"task type {task_type} not found under task_types")
        tt_end = _block_end(lines, tt, _indent(lines[tt]))
        target = next((i for i in range(tt + 1, tt_end) if "tier_by_complexity:" in lines[i]
                       and lines[i].lstrip().startswith("tier_by_complexity:")), None)
        if target is None:
            raise PatchError(f"task type {task_type} has no tier_by_complexity")
        if _FLOW.match(lines[target].rstrip("\n")):
            lines[target] = _edit_flow(lines[target], updates)
        elif _BLOCK_START.match(lines[target].rstrip("\n")):
            _edit_block(lines, target, updates)
        else:
            raise PatchError(f"unsupported tier_by_complexity layout for {task_type}")
    new = "".join(lines)
    _verify(new, changes)
    return new


def _verify(text: str, changes: list[Change]) -> None:
    data = yaml.safe_load(text) or {}
    for c in changes:
        got = ((data.get("task_types") or {}).get(c.task_type) or {}).get("tier_by_complexity", {}).get(c.complexity)
        if got != c.new:
            raise PatchError(f"patched routing.yaml has {c.task_type}.{c.complexity}={got!r}, wanted {c.new!r}")


def unified_diff(old: str, new: str, path: str = "config/routing.yaml") -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))
