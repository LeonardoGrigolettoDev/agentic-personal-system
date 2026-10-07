from pathlib import Path

import pytest


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def make_vault(tmp_path):
    """Create an Obsidian vault (dir with .obsidian/) under tmp_path."""

    def _make(name: str, parent: Path | None = None) -> Path:
        vault = (parent or tmp_path) / name
        (vault / ".obsidian").mkdir(parents=True)
        return vault

    return _make
