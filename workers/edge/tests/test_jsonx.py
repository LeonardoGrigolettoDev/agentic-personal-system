import pytest

from edge.jsonx import JSONExtractionError, extract_json_object

GOOD = {"summary": "ok", "notes": ["a"], "tasks": [], "topics": ["x"]}


@pytest.mark.parametrize(
    "raw",
    [
        '{"summary": "ok", "notes": ["a"], "tasks": [], "topics": ["x"]}',
        '```json\n{"summary": "ok", "notes": ["a"], "tasks": [], "topics": ["x"]}\n```',
        'Claro! Aqui está o JSON:\n```\n{"summary": "ok", "notes": ["a"], "tasks": [], "topics": ["x"]}\n```\nEspero ter ajudado.',
        '<think>vou pensar {"rascunho": 1}</think>\n{"summary": "ok", "notes": ["a"], "tasks": [], "topics": ["x"]}',
        'Resultado: {"summary": "ok", "notes": ["a",], "tasks": [], "topics": ["x"],} fim',
        '{\n  // comentário\n  "summary": "ok", "notes": ["a"], "tasks": [], "topics": ["x"]\n}',
        "{“summary”: “ok”, “notes”: [“a”], “tasks”: [], “topics”: [“x”]}",
    ],
)
def test_messy_outputs(raw):
    assert extract_json_object(raw) == GOOD


def test_python_literals_outside_strings_only():
    obj = extract_json_object('{"summary": "True story", "done": True, "owner": None}')
    assert obj == {"summary": "True story", "done": True, "owner": None}


def test_braces_inside_strings_do_not_confuse_the_scanner():
    raw = 'prefixo {"summary": "use {chaves} e \\"aspas\\"", "notes": []} sufixo {"outro": 1}'
    assert extract_json_object(raw) == {"summary": 'use {chaves} e "aspas"', "notes": []}


def test_truncated_output_is_closed():
    raw = '{"summary": "reunião sobre backup", "notes": ["primeiro ponto", "segundo pon'
    assert extract_json_object(raw) == {"summary": "reunião sobre backup", "notes": ["primeiro ponto", "segundo pon"]}
    cut_after_key = '{"summary": "x", "notes": ["a"], "tasks'
    assert extract_json_object(cut_after_key) == {"summary": "x", "notes": ["a"]}


def test_unterminated_think_block():
    assert extract_json_object('<think>pensando sem fim... {"summary": "ok"}') == {"summary": "ok"}


@pytest.mark.parametrize("raw", ["", "   ", "Desculpe, não posso ajudar.", "[1, 2, 3]", "{sem: aspas}"])
def test_no_object_raises(raw):
    with pytest.raises(JSONExtractionError):
        extract_json_object(raw)
