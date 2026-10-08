"""Load bench tasks from YAML and lint them (JSON schema + cross-file rules)."""

import json
import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path, PurePosixPath

import yaml
from jsonschema import Draft202012Validator, SchemaError

from bench import CATEGORIES
from bench.fixtures_gen import GENERATED

WORKDIR = "{workdir}"


@dataclass(frozen=True)
class Task:
    key: str
    category: str
    title: str
    prompt: str
    tenant: str
    check: dict
    path: Path
    agent: str | None = None
    fixture: str | None = None
    setup: str | None = None
    generated_files: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    timeout_s: int | None = None
    expected_task_type: str | None = None
    expected_domain: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_workdir(self) -> bool:
        return bool(self.fixture) or self.check.get("type") == "command"

    def render_prompt(self, workdir: str | None) -> str:
        if WORKDIR in self.prompt:
            if not workdir:
                raise ValueError(f"task {self.key} needs a workdir")
            return self.prompt.replace(WORKDIR, workdir)
        return self.prompt


class TaskError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@cache
def schema() -> dict:
    return json.loads(resources.files("bench").joinpath("task.schema.json").read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(schema())


def task_files(tasks_dir: Path) -> list[Path]:
    return sorted(p for p in tasks_dir.glob("*/*.y*ml") if p.suffix in (".yaml", ".yml"))


def parse_file(path: Path) -> tuple[Task | None, list[str]]:
    """Parse and schema-check one file. Returns (task, problems)."""
    where = f"{path.parent.name}/{path.name}"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, [f"{where}: unreadable YAML: {exc}"]
    if not isinstance(data, dict):
        return None, [f"{where}: top level must be a mapping"]
    errors = sorted(_validator().iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        return None, [f"{where}: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors]
    task = Task(
        key=data["key"], category=data["category"], title=data["title"], prompt=data["prompt"],
        tenant=data["tenant"], check=data["check"], path=path, agent=data.get("agent"),
        fixture=data.get("fixture"), setup=data.get("setup"),
        generated_files=tuple(data.get("generated_files") or ()), requires=tuple(data.get("requires") or ()),
        timeout_s=data.get("timeout_s"),
        expected_task_type=data.get("expected_task_type"), expected_domain=data.get("expected_domain"),
        tags=tuple(data.get("tags") or ()),
    )
    return task, []


def load_tasks(tasks_dir: Path) -> list[Task]:
    """All tasks, ordered by category then key. Raises TaskError on schema problems."""
    tasks, problems = [], []
    for path in task_files(tasks_dir):
        task, errs = parse_file(path)
        problems += errs
        if task:
            tasks.append(task)
    if problems:
        raise TaskError(problems)
    order = list(CATEGORIES)
    return sorted(tasks, key=lambda t: (order.index(t.category), t.key))


def select(tasks: list[Task], categories: list[str] | None = None, keys: list[str] | None = None) -> list[Task]:
    if keys:
        known = {t.key for t in tasks}
        missing = sorted(set(keys) - known)
        if missing:
            raise TaskError([f"unknown task key(s): {', '.join(missing)}"])
    return [t for t in tasks if (not categories or t.category in categories) and (not keys or t.key in keys)]


# ---------------------------------------------------------------- lint (bench validate)

def load_routing(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    return data if isinstance(data, dict) else {}


def load_agents(agents_dir: Path) -> dict[str, dict]:
    agents = {}
    for path in sorted(agents_dir.glob("*/agent.yaml")) if agents_dir.is_dir() else []:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        agents[data.get("agent") or path.parent.name] = data
    return agents


def lint(tasks_dir: Path, fixtures_dir: Path, routing: dict | None = None,
         agents: dict[str, dict] | None = None) -> tuple[list[Task], list[str]]:
    """Every rule `bench validate` enforces. Returns (tasks that parsed, problems)."""
    tasks, problems = [], []
    for path in task_files(tasks_dir):
        task, errs = parse_file(path)
        problems += errs
        if task:
            tasks.append(task)
            problems += [f"{task.key}: {p}" for p in _task_rules(task, fixtures_dir, routing, agents)]

    seen: dict[str, Path] = {}
    for t in tasks:
        if t.key in seen:
            problems.append(f"{t.key}: duplicate key ({seen[t.key]} and {t.path})")
        seen[t.key] = t.path
    counts = {c: 0 for c in CATEGORIES}
    for t in tasks:
        counts[t.category] += 1
    for cat, want in CATEGORIES.items():
        if counts[cat] != want:
            problems.append(f"category {cat}: {counts[cat]} tasks, expected {want}")
    if tasks_dir.is_dir():
        for extra in sorted(p.name for p in tasks_dir.iterdir() if p.is_dir() and p.name not in CATEGORIES):
            problems.append(f"tasks/{extra}: unknown category directory")
    return tasks, problems


def _task_rules(t: Task, fixtures_dir: Path, routing: dict | None, agents: dict[str, dict] | None) -> list[str]:
    out = []
    if t.path.stem != t.key:
        out.append(f"file name {t.path.name} must be {t.key}.yaml")
    if t.path.parent.name != t.category:
        out.append(f"lives in tasks/{t.path.parent.name}/ but category is {t.category}")

    uses_workdir = WORKDIR in t.prompt
    if t.fixture:
        fdir = fixtures_dir / t.fixture
        if not fdir.is_dir():
            out.append(f"fixture dir {fdir} does not exist")
        elif not any(fdir.iterdir()):
            out.append(f"fixture dir {fdir} is empty")
        if not uses_workdir:
            out.append(f"has a fixture but the prompt never mentions {WORKDIR}")
    elif uses_workdir:
        out.append(f"prompt uses {WORKDIR} but the task has no fixture")
    if t.check["type"] == "command" and not t.fixture:
        out.append("command checks need a fixture (the workdir they run in)")
    if t.setup and not t.fixture:
        out.append("setup needs a fixture")

    for rel in t.check.get("protect", []):
        if ".." in PurePosixPath(rel).parts:
            out.append(f"protect path {rel} escapes the workdir")
        elif t.fixture and not (fixtures_dir / t.fixture / rel).is_file():
            out.append(f"protect path {rel} is not a file in the fixture")

    if t.generated_files:
        declared = {g.name for g in GENERATED.get(t.fixture or "", [])}
        for name in t.generated_files:
            if name not in declared:
                out.append(f"generated file {name} is not produced by `bench fixtures` for fixture {t.fixture}")

    if t.check["type"] == "regex":
        try:
            re.compile(t.check["pattern"])
        except re.error as exc:
            out.append(f"regex does not compile: {exc}")
    elif t.check["type"] == "json_schema":
        try:
            Draft202012Validator.check_schema(t.check["schema"])
        except SchemaError as exc:
            out.append(f"check schema is not a valid JSON schema: {getattr(exc, 'message', exc)}")

    if routing is not None and t.expected_task_type:
        task_types = routing.get("task_types") or {}
        if t.expected_task_type not in task_types:
            out.append(f"expected_task_type {t.expected_task_type} is not in routing.yaml task_types")
    if agents is not None:
        out += _agent_rules(t, agents)
    return out


def _agent_rules(t: Task, agents: dict[str, dict]) -> list[str]:
    """The target profile must be allowed to touch the task's tenant and the tools its workdir needs: a run cannot
    delegate to a Kanban worker and still be checked when it finishes (delegation is asynchronous)."""
    name = t.agent or "chief"
    agent = agents.get(name)
    if agent is None:
        return [f"agent {name} has no agents/{name}/agent.yaml"] if t.agent else []
    out = []
    if t.tenant != "shared" and t.tenant not in (agent.get("allowed_tenants") or []):
        out.append(f"agent {name} may not access tenant {t.tenant}")
    tools = set(agent.get("allowed_tools") or [])
    if t.check["type"] == "command" and "shell" not in tools:
        out.append(f"command check needs a profile with the shell tool category; {name} has {sorted(tools)}")
    elif t.fixture and not tools & {"shell", "filesystem"}:
        out.append(f"fixture needs a profile with shell or filesystem tools; {name} has {sorted(tools)}")
    return out
