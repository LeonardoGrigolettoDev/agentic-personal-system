import re

import pytest
from conftest import AGENTS_DIR, FIXTURES_DIR, TASKS_DIR, write_task

from bench import CATEGORIES
from bench.tasks import TaskError, lint, load_tasks, parse_file, select, task_files


def test_every_task_passes_lint(routing, agents):
    tasks, problems = lint(TASKS_DIR, FIXTURES_DIR, routing, agents)
    assert problems == []
    assert len(tasks) == len(task_files(TASKS_DIR))


def test_category_counts_match_the_battery(all_tasks):
    counts = {c: sum(t.category == c for t in all_tasks) for c in CATEGORIES}
    assert counts == {"simple": 10, "medium": 10, "debugging": 10, "refactor": 5,
                      "agentic": 5, "research": 5, "finance": 5, "media": 5}
    assert len(all_tasks) == sum(CATEGORIES.values())


def test_keys_are_unique_and_match_files(all_tasks):
    keys = [t.key for t in all_tasks]
    assert len(keys) == len(set(keys))
    for t in all_tasks:
        assert t.path.stem == t.key and t.path.parent.name == t.category


def test_prompts_are_portuguese_and_workdir_consistent(all_tasks):
    for t in all_tasks:
        assert re.search(r"[áéíóúãõçê]", t.prompt, re.IGNORECASE), f"{t.key}: prompt should be pt-BR"
        assert ("{workdir}" in t.prompt) == bool(t.fixture), t.key


def test_every_check_type_and_router_label_is_used(all_tasks):
    assert {t.check["type"] for t in all_tasks} == {"command", "regex", "json_schema", "llm_judge"}
    assert sum(1 for t in all_tasks if t.expected_task_type) >= 40
    assert sum(1 for t in all_tasks if t.expected_domain) >= 30


def test_fixture_generated_media_is_declared(all_tasks):
    media = [t for t in all_tasks if t.generated_files]
    assert {t.key for t in media} == {"media-metadados-audio", "media-recado-whatsapp", "media-aula-concorrencia",
                                     "media-video-aula-ingles"}


def test_transcription_tasks_require_the_edge_pipeline(all_tasks):
    edge = {t.key for t in all_tasks if "edge" in t.requires}
    assert edge == {t.key for t in all_tasks if "transcript_to_notes" in t.prompt}
    assert edge == {"media-recado-whatsapp", "media-aula-concorrencia", "media-video-aula-ingles"}


def test_python_fixtures_give_the_aios_gate_a_test_target(all_tasks):
    for t in all_tasks:
        if t.check["type"] == "command" and t.check["run"].startswith("python3 -m unittest") or \
                t.key.startswith("refactor-py-"):
            assert (FIXTURES_DIR / t.fixture / "Makefile").read_text().count("python3 -m unittest -q") == 1, t.key
            assert "Makefile" in t.check["protect"], t.key


def test_render_prompt_replaces_workdir(task_by_key):
    t = task_by_key["debug-py-paginacao"]
    assert "/ws/bench-x" in t.render_prompt("/ws/bench-x")
    with pytest.raises(ValueError):
        t.render_prompt(None)


def test_select_by_category_and_key(all_tasks):
    assert {t.category for t in select(all_tasks, ["finance"])} == {"finance"}
    assert [t.key for t in select(all_tasks, None, ["simple-juros-compostos"])] == ["simple-juros-compostos"]
    with pytest.raises(TaskError):
        select(all_tasks, None, ["nao-existe"])


BASE = {"key": "simple-x", "category": "simple", "title": "Tarefa x", "tenant": "pessoal",
        "prompt": "Responda com uma frase em português, por favor.", "check": {"type": "regex", "pattern": "x"}}


@pytest.mark.parametrize("change, message", [
    ({"extra": 1}, "Additional properties"),
    ({"check": {"type": "regex"}}, "'pattern' is a required property"),
    ({"check": {"type": "command", "run": "true", "bogus": 1}}, "Additional properties"),
    ({"check": {"type": "llm_judge", "rubric": "curta", "pass_score": 7}}, "is too short"),
    ({"check": {"type": "llm_judge", "rubric": "uma rubrica longa o bastante", "pass_score": 11}}, "maximum"),
    ({"check": {"type": "telepathy"}}, "is not one of"),
    ({"tenant": "empresa"}, "is not one of"),
    ({"key": "Simple_X"}, "does not match"),
    ({"requires": ["gpu"]}, "is not one of"),
])
def test_schema_rejects_bad_tasks(tmp_path, change, message):
    path = write_task(tmp_path, {**BASE, **change})
    task, problems = parse_file(path)
    assert task is None
    assert any(message in p for p in problems), problems


def test_lint_cross_file_rules(tmp_path, routing, agents):
    tasks_dir, fixtures_dir = tmp_path / "tasks", tmp_path / "fixtures"
    (fixtures_dir / "simple-fx").mkdir(parents=True)
    (fixtures_dir / "simple-fx" / "a.txt").write_text("x")
    write_task(tasks_dir, {**BASE, "key": "simple-fx", "fixture": "simple-fx"})  # fixture but no {workdir}
    write_task(tasks_dir, {**BASE, "key": "simple-nofx", "prompt": "Veja {workdir} e responda já."})
    write_task(tasks_dir, {**BASE, "key": "simple-cmd", "check": {"type": "command", "run": "true"}})
    write_task(tasks_dir, {**BASE, "key": "simple-re", "check": {"type": "regex", "pattern": "(unclosed"}})
    write_task(tasks_dir, {**BASE, "key": "simple-js", "check": {"type": "json_schema", "schema": {"type": 5}}})
    write_task(tasks_dir, {**BASE, "key": "simple-tt", "expected_task_type": "astrologia"})
    write_task(tasks_dir, {**BASE, "key": "simple-ag", "agent": "personal", "tenant": "nitro"})
    write_task(tasks_dir, {**BASE, "key": "simple-nofs", "agent": "personal", "fixture": "simple-fx",
                           "prompt": "Leia {workdir}/a.txt e responda."})
    write_task(tasks_dir, {**BASE, "key": "simple-pr", "fixture": "simple-fx", "prompt": "Arquivos em {workdir} aqui.",
                           "check": {"type": "command", "run": "true", "protect": ["nao.txt"]}})
    write_task(tasks_dir, {**BASE, "key": "simple-gen", "fixture": "simple-fx", "prompt": "Arquivo {workdir}/a.wav aqui.",
                           "generated_files": ["a.wav"]})
    path = write_task(tasks_dir, {**BASE, "key": "simple-dup"})
    path.rename(path.with_name("outro-nome.yaml"))
    _, problems = lint(tasks_dir, fixtures_dir, routing, agents)
    text = "\n".join(problems)
    for expected in ["prompt never mentions {workdir}", "prompt uses {workdir} but the task has no fixture",
                     "command checks need a fixture", "regex does not compile", "not a valid JSON schema",
                     "expected_task_type astrologia", "may not access tenant nitro", "protect path nao.txt",
                     "not produced by `bench fixtures`", "file name outro-nome.yaml", "category medium: 0 tasks",
                     "category media: 0 tasks, expected 5", "command check needs a profile with the shell",
                     "fixture needs a profile with shell or filesystem tools; personal"]:
        assert expected in text, expected


def test_load_tasks_raises_with_every_problem(tmp_path):
    write_task(tmp_path, {**BASE, "key": "simple-a", "tenant": "x"})
    write_task(tmp_path, {**BASE, "key": "simple-b", "check": {"type": "regex"}})
    with pytest.raises(TaskError) as exc:
        load_tasks(tmp_path)
    assert len(exc.value.problems) == 2


def test_agents_dir_is_the_repo_one():
    assert (AGENTS_DIR / "engineering" / "agent.yaml").is_file()
