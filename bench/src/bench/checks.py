"""Check evaluators: command (in the workspace), regex / json_schema (on the final answer), llm_judge."""

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator

from bench.workspace import Workspace, WorkspaceError

DEFAULT_COMMAND_TIMEOUT = 600


@dataclass
class CheckResult:
    passed: bool
    detail: dict = field(default_factory=dict)
    error: str | None = None  # infrastructure failure: the result is not a verdict on the agent


class Judge(Protocol):
    def score(self, *, prompt: str, rubric: str, answer: str) -> tuple[float, str]: ...


class JudgeError(Exception):
    pass


# ---------------------------------------------------------------- number normalisation (pt-BR and en)

# a number may end a sentence ("R$ 1.234,56."), so only a following digit or separator+digit blocks a match
_EN_THOUSANDS_DEC = re.compile(r"(?<![\d.,])[1-9]\d{0,2}(?:,\d{3})+\.\d+(?!\d|[.,]\d)")
_BR_THOUSANDS_DEC = re.compile(r"(?<![\d.,])[1-9]\d{0,2}(?:\.\d{3})+,\d+(?!\d|[.,]\d)")
_BR_THOUSANDS = re.compile(r"(?<![\d.,])[1-9]\d{0,2}(?:\.\d{3})+(?!\d|[.,]\d)")
_DECIMAL_COMMA = re.compile(r"(?<![\d.,])(\d+),(\d+)(?!\d|[.,]\d)")


def normalize_numbers(text: str) -> str:
    """'R$ 1.234,56' -> 'R$ 1234.56', '1,234.56' -> '1234.56', '4.200' -> '4200', '12,5%' -> '12.5%'."""
    text = _EN_THOUSANDS_DEC.sub(lambda m: m.group(0).replace(",", ""), text)
    text = _BR_THOUSANDS_DEC.sub(lambda m: m.group(0).replace(".", "").replace(",", "."), text)
    text = _BR_THOUSANDS.sub(lambda m: m.group(0).replace(".", ""), text)
    return _DECIMAL_COMMA.sub(r"\1.\2", text)


# ---------------------------------------------------------------- JSON extraction

_FENCE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\n(.*?)```", re.DOTALL)


def json_candidates(text: str):
    """JSON values in preference order: fenced blocks, the whole answer, then each parseable {...}/[...]."""
    for chunk in [m.group(1) for m in _FENCE.finditer(text)] + [text]:
        try:
            yield json.loads(chunk.strip())
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                yield decoder.raw_decode(text, i)[0]
            except ValueError:
                continue


def extract_json(text: str):
    for value in json_candidates(text):
        return value
    raise ValueError("no JSON value found in the answer")


# ---------------------------------------------------------------- protected files

def sha256_files(root: Path, paths: list[str]) -> dict[str, str]:
    return {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}


def protect_command(hashes: dict[str, str]) -> str:
    lines = " ".join(shlex.quote(f"{h}  {p}") for p, h in sorted(hashes.items()))
    return f"printf '%s\\n' {lines} | sha256sum -c --quiet -"


# ---------------------------------------------------------------- evaluators

def check_regex(check: dict, answer: str) -> CheckResult:
    text = normalize_numbers(answer) if check.get("normalize_numbers") else answer
    m = re.search(check["pattern"], text)
    detail = {"pattern": check["pattern"], "normalized": bool(check.get("normalize_numbers"))}
    if m:
        detail["match"] = m.group(0)[:200]
    return CheckResult(bool(m), detail)


def check_json_schema(check: dict, answer: str, max_candidates: int = 20) -> CheckResult:
    """Passes when any JSON value in the answer validates; otherwise reports the first candidate's errors."""
    validator = Draft202012Validator(check["schema"])
    first_errors = None
    for n, value in enumerate(json_candidates(answer)):
        if n >= max_candidates:
            break
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
        if not errors:
            return CheckResult(True, {"value": json.dumps(value, ensure_ascii=False)[:1000]})
        if first_errors is None:
            first_errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"[:300]
                            for e in errors[:5]]
    if first_errors is None:
        return CheckResult(False, {"reason": "no JSON value found in the answer"})
    return CheckResult(False, {"errors": first_errors})


def check_command(check: dict, workspace: Workspace | None, workdir: str | None,
                  protect_hashes: dict[str, str] | None) -> CheckResult:
    if workspace is None or not workdir:
        return CheckResult(False, {}, error="command check without a workspace")
    timeout = float(check.get("timeout_s") or DEFAULT_COMMAND_TIMEOUT)
    expect = int(check.get("expect_exit", 0))
    detail: dict = {"command": check["run"][:500], "expect_exit": expect}
    try:
        if protect_hashes:
            guard = workspace.run(workdir, protect_command(protect_hashes), 60)
            detail["protected"] = sorted(protect_hashes)
            if guard.exit_code != 0:
                detail.update(stage="protect", exit_code=guard.exit_code, output_tail=guard.output_tail)
                return CheckResult(False, detail)
        res = workspace.run(workdir, check["run"], timeout)
    except WorkspaceError as exc:
        return CheckResult(False, detail, error=str(exc))
    detail.update(stage="run", exit_code=res.exit_code, output_tail=res.output_tail, timed_out=res.timed_out)
    return CheckResult(res.exit_code == expect, detail)


def check_llm_judge(check: dict, answer: str, prompt: str, judge: Judge | None) -> CheckResult:
    if judge is None:
        return CheckResult(False, {}, error="llm_judge check but no judge configured (BENCH_LITELLM_KEY)")
    try:
        score, reason = judge.score(prompt=prompt, rubric=check["rubric"], answer=answer)
    except JudgeError as exc:
        return CheckResult(False, {"pass_score": check["pass_score"]}, error=f"judge failed: {exc}")
    return CheckResult(score >= float(check["pass_score"]),
                       {"score": score, "pass_score": check["pass_score"], "reason": reason[:1000]})


def evaluate(check: dict, *, answer: str, prompt: str = "", workspace: Workspace | None = None,
             workdir: str | None = None, judge: Judge | None = None,
             protect_hashes: dict[str, str] | None = None) -> CheckResult:
    kind = check["type"]
    if kind == "command":
        result = check_command(check, workspace, workdir, protect_hashes)
    elif not answer.strip():
        result = CheckResult(False, {"reason": "empty final answer"})
    elif kind == "regex":
        result = check_regex(check, answer)
    elif kind == "json_schema":
        result = check_json_schema(check, answer)
    elif kind == "llm_judge":
        result = check_llm_judge(check, answer, prompt, judge)
    else:
        result = CheckResult(False, {}, error=f"unknown check type {kind}")
    result.detail = {"type": kind, **result.detail}
    return result
