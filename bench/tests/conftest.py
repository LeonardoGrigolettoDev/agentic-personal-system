import shutil
from pathlib import Path

import pytest
import yaml

from bench.tasks import load_agents, load_routing, load_tasks

BENCH = Path(__file__).resolve().parents[1]
REPO = BENCH.parent
TASKS_DIR = BENCH / "tasks"
FIXTURES_DIR = BENCH / "fixtures"
SOLUTIONS_DIR = Path(__file__).resolve().parent / "solutions"
ROUTING_FILE = REPO / "config" / "routing.yaml"
AGENTS_DIR = REPO / "agents"


@pytest.fixture(scope="session")
def all_tasks():
    return load_tasks(TASKS_DIR)


@pytest.fixture(scope="session")
def task_by_key(all_tasks):
    return {t.key: t for t in all_tasks}


@pytest.fixture(scope="session")
def routing():
    return load_routing(ROUTING_FILE)


@pytest.fixture(scope="session")
def agents():
    return load_agents(AGENTS_DIR)


@pytest.fixture(scope="session")
def golden():
    return yaml.safe_load((Path(__file__).parent / "golden_answers.yaml").read_text(encoding="utf-8"))


def write_task(directory: Path, data: dict) -> Path:
    path = directory / data["category"] / f"{data['key']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def apply_overlay(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)
