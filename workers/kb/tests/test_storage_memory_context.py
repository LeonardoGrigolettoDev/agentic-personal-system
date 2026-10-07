import pytest

from kb import context, memory, storage
from kb.context import ContextRequest, _Budget


@pytest.mark.parametrize("uri", [
    "storage://media/a/b.wav", "storage://documents/x.pdf",
])
def test_parse_uri_ok(uri):
    bucket, key = storage.parse_uri(uri)
    assert storage.make_uri(bucket, key) == uri


@pytest.mark.parametrize("uri", [
    "file:///etc/passwd", "storage://media/../secret", "storage://Media/x", "storage://media/", "storage://media//abs",
    "storage://media/a\\b",
])
def test_parse_uri_rejects(uri):
    with pytest.raises(storage.StorageError):
        storage.parse_uri(uri)


def test_local_storage_roundtrip(tmp_path):
    s = storage.LocalStorage(tmp_path)
    uri = s.put("storage://media/input/a.txt", b"hello")
    assert s.get(uri) == b"hello" and s.exists(uri)
    assert list(s.list("storage://media/input")) == [uri]
    assert s.path(uri).is_relative_to(tmp_path)
    s.delete(uri)
    assert not s.exists(uri)


def test_storage_from_env(tmp_path):
    assert isinstance(storage.from_env({"STORAGE_BACKEND": "local", "STORAGE_LOCAL_ROOT": str(tmp_path)}),
                      storage.LocalStorage)
    with pytest.raises(storage.StorageError):
        storage.from_env({"STORAGE_BACKEND": "ftp"})


@pytest.mark.parametrize("kwargs, msg", [
    ({"tenant": "x"}, "tenant"),
    ({"scope": "domain"}, "domain"),
    ({"scope": "project"}, "project"),
    ({"kind": "gossip"}, "kind"),
    ({"lifecycle": "deprecated"}, "lifecycle"),
    ({"content": "  "}, "empty"),
    ({"content": "x" * 5000}, "document"),
    ({"importance": 2}, "importance"),
])
def test_memory_validation(kwargs, msg):
    base = {"tenant": "pessoal", "scope": "global", "kind": "preference", "content": "prefere respostas curtas"}
    with pytest.raises(memory.MemoryInputError, match=msg):
        memory.MemoryIn(**{**base, **kwargs}).validate()


def test_context_request_validation():
    with pytest.raises(ValueError):
        ContextRequest(task=" ", tenant="pessoal").validate()
    with pytest.raises(ValueError):
        ContextRequest(task="x", tenant="pessoal", budget_tokens=10).validate()
    with pytest.raises(ValueError):
        ContextRequest(task="x", tenant="pessoal", domain="sales").validate()


def test_budget_never_exceeds_total():
    b = _Budget(100)
    taken = [b.take("palavra " * n) for n in (10, 30, 100, 3)]
    assert taken == [True, True, False, True]
    assert b.used <= 100 and b.dropped == 1


def test_render_has_stable_sections():
    ctx = {
        "tenant": "pessoal", "domain": "learning",
        "project": {"name": "Inglês", "slug": "ingles", "status": "active", "description": "C1 até 2027",
                    "current_state": "B2"},
        "constraints": [{"kind": "rule", "content": "estudar 30min/dia"}],
        "relevant_decisions": [], "relevant_memories": [{"kind": "fact", "content": "usa Anki"}],
        "relevant_documents": [{"title": "Plano", "source_uri": "obsidian://Pessoal/plano.md", "heading_path": None,
                                "content": "..."}],
    }
    md = context.render(ctx)
    assert md.index("Restrições") < md.index("Memórias") < md.index("Documentos")
    assert "Estado atual: B2" in md
