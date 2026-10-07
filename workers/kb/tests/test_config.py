from kb.config import Settings, find_env_file, load_dotenv, parse_dotenv


def test_parse_dotenv_handles_quotes_comments_and_export():
    text = """
# comment
export A=1
B = "two words"  
C='single # not comment'
D=value # trailing comment
E=
not a valid line
F="line\\nbreak"
"""
    assert parse_dotenv(text) == {
        "A": "1", "B": "two words", "C": "single # not comment", "D": "value", "E": "", "F": "line\nbreak",
    }


def test_load_dotenv_never_overrides_real_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KB_EMBED_MODEL=from-file\nKB_EMBED_DIM=1024\nKB_LITELLM_KEY=file-key\n")
    environ = {"KB_EMBED_MODEL": "from-env", "KB_LITELLM_KEY": ""}
    applied = load_dotenv(env_file, environ)
    assert applied == {"KB_EMBED_DIM": "1024"}
    assert environ["KB_EMBED_MODEL"] == "from-env"
    assert environ["KB_LITELLM_KEY"] == ""  # set-but-empty still wins


def test_default_database_url_built_from_env_parts():
    s = Settings.from_env({"AIOS_DB_PASSWORD": "p@ss/w:rd", "PG_HOST_PORT": "5544"})
    assert s.database_url == "postgresql://aios:p%40ss%2Fw%3Ard@127.0.0.1:5544/aios"
    assert s.litellm_base_url == "http://127.0.0.1:4000"
    assert (s.embed_model, s.embed_model_name, s.embed_dim, s.embed_batch_size) == (
        "embed-local", "qwen3-embedding:0.6b", 1024, 16,
    )
    assert s.litellm_key is None


def test_explicit_database_url_wins():
    s = Settings.from_env({"KB_DATABASE_URL": "postgresql://x@db/aios", "AIOS_DB_PASSWORD": "ignored"})
    assert s.database_url == "postgresql://x@db/aios"


def test_find_env_file_requires_compose_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("KB_ENV_FILE", raising=False)
    repo = tmp_path / "repo"
    nested = repo / "workers" / "kb"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("X=1")  # no compose.yaml next to it -> ignored
    assert find_env_file([nested]) is None
    (repo / "compose.yaml").write_text("services: {}")
    (repo / ".env").write_text("X=2")
    assert find_env_file([nested]) == repo / ".env"
