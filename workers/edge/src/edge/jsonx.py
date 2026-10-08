"""Pull one JSON object out of messy LLM output.

Handles: <think> blocks, ```json fences, prose around the object, trailing commas, // comments,
smart quotes, Python literals and output truncated by max_tokens (unclosed strings/brackets).
"""

import json
import re

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\n?(.*?)```", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}
_MAX_CANDIDATES = 20


class JSONExtractionError(ValueError):
    pass


def extract_json_object(text: str) -> dict:
    if not text or not text.strip():
        raise JSONExtractionError("resposta vazia do modelo")
    cleaned = _THINK.sub("", text)
    if "<think>" in cleaned.lower():  # unterminated reasoning block: keep what follows the last tag
        cleaned = re.split(r"(?i)<think>", cleaned)[-1]
    cleaned = cleaned.strip()

    for candidate in _candidates(cleaned):
        obj = _loads_lenient(candidate)
        if isinstance(obj, dict):
            return obj
    raise JSONExtractionError("nenhum objeto JSON válido na resposta do modelo")


def _candidates(text: str):
    yield text
    for block in _FENCE.findall(text):
        yield block.strip()
    starts = [i for i, ch in enumerate(text) if ch == "{"][:_MAX_CANDIDATES]
    yield from (_balanced_from(text, start) for start in starts)


def _balanced_from(text: str, start: int) -> str:
    """Substring from `start` to its matching brace (string-aware); the rest of the text if unbalanced."""
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _loads_lenient(candidate: str):
    for variant in _variants(candidate):
        try:
            return json.loads(variant)
        except (json.JSONDecodeError, RecursionError):
            continue
    return None


def _variants(s: str):
    yield s
    fixed = _TRAILING_COMMA.sub(r"\1", _LINE_COMMENT.sub("", s))
    yield fixed
    loose = _replace_outside_strings(fixed.replace("“", '"').replace("”", '"'))
    yield loose
    yield from _close_truncated(loose)


def _replace_outside_strings(s: str) -> str:
    """Python literals -> JSON, only outside string values."""
    out, in_str, escaped, i = [], False, False, 0
    while i < len(s):
        ch = s[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        for py, js in _PY_LITERALS.items():
            if (
                s.startswith(py, i)
                and not (i and s[i - 1].isalnum())
                and not s[i + len(py) : i + len(py) + 1].isalnum()
            ):
                out.append(js)
                i += len(py)
                break
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _close_truncated(s: str, attempts: int = 6):
    """Close an object cut off mid-way; on failure, drop the last (partial) member and retry."""
    text = s.rstrip()
    for _ in range(attempts):
        closed = _close(text)
        if closed is None:
            return
        yield closed
        cut = text.rfind(",")
        if cut <= 0:
            return
        text = text[:cut]


def _close(s: str) -> str | None:
    stack, in_str, escaped = [], False, False
    for ch in s:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    if not stack and not in_str:
        return None  # already balanced: nothing to close
    tail = '"' if in_str else ""
    body = (s + tail).rstrip()
    body = re.sub(r"[,:]\s*$", "", body)
    return body + "".join(reversed(stack))
