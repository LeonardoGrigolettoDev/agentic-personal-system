import pytest

from kb.ingest import IngestOptions, prepare
from kb.sources import (
    TenantGuardError,
    check_tenant,
    discover,
    find_vault_root,
    prune_prefixes,
    resolve_source,
)
from tests.conftest import write


def test_vault_uri_is_relative_to_vault(make_vault):
    vault = make_vault("Pessoal")
    note = write(vault / "03 Áreas" / "Saúde.md", "x")
    ref = resolve_source(note, vault / "03 Áreas")
    assert ref.uri == "obsidian://Pessoal/03 Áreas/Saúde.md"
    assert (ref.source_type, ref.vault) == ("obsidian", "Pessoal")


def test_file_uri_uses_root_label_or_dir_name(tmp_path):
    root = tmp_path / "inbox"
    f = write(root / "sub" / "aula.txt", "x")
    assert resolve_source(f, root).uri == "file://inbox/sub/aula.txt"
    assert resolve_source(f, root, label="cursos").uri == "file://cursos/sub/aula.txt"
    assert resolve_source(f, f).uri == "file://sub/aula.txt"  # single file: parent dir is the root


def test_uris_never_contain_host_paths(tmp_path, make_vault):
    vault = make_vault("Nitro")
    files = [write(vault / "a.md", "x"), write(tmp_path / "docs" / "b.md", "x")]
    for f in files:
        uri = resolve_source(f, f.parent).uri
        assert str(tmp_path) not in uri and "/tmp" not in uri and "//" not in uri.split("://", 1)[1]


def test_discover_skips_obsidian_internals_templates_and_hidden(make_vault):
    vault = make_vault("Pessoal")
    keep = write(vault / "Notas" / "a.md", "x")
    for p in [".obsidian/workspace.json", ".trash/old.md", "Templates/t.md", "Notas/Templates/t2.md",
              ".git/config", "Notas/.hidden.md"]:
        write(vault / p, "x")
    assert discover(vault).files == [keep]


def test_find_vault_root_walks_up(make_vault):
    vault = make_vault("Pessoal")
    note = write(vault / "a" / "b" / "c.md", "x")
    assert find_vault_root(note) == vault.resolve()
    assert find_vault_root(vault.parent) is None


@pytest.mark.parametrize(
    ("vault", "tenant", "allowed"),
    [
        ("Nitro", "nitro", True),
        ("Nitro", "pessoal", False),
        ("Nitro", "shared", False),
        ("Pessoal", "pessoal", True),
        ("Pessoal", "nitro", False),
        ("Pessoal", "shared", False),
        ("pessoal", "nitro", False),  # case-insensitive
        ("Outro", "nitro", True),
        (None, "shared", True),
    ],
)
def test_check_tenant(vault, tenant, allowed):
    if allowed:
        check_tenant(vault, tenant)
    else:
        with pytest.raises(TenantGuardError):
            check_tenant(vault, tenant)


def test_guard_refuses_subfolder_and_single_file_of_wrong_vault(make_vault):
    vault = make_vault("Nitro")
    note = write(vault / "Clientes" / "x.md", "x")
    for target in (vault, vault / "Clientes", note):
        with pytest.raises(TenantGuardError):
            prepare(target, IngestOptions(tenant="pessoal", dry_run=True))
    prepare(vault, IngestOptions(tenant="nitro", dry_run=True))


def test_guard_refuses_parent_dir_containing_other_tenant_vault(tmp_path, make_vault):
    obsidian = tmp_path / "Obsidian"
    write(make_vault("Pessoal", obsidian) / "p.md", "x")
    write(make_vault("Nitro", obsidian) / "n.md", "x")
    with pytest.raises(TenantGuardError):
        prepare(obsidian, IngestOptions(tenant="pessoal", dry_run=True))


def test_guard_checks_empty_vault_below_root(tmp_path, make_vault):
    make_vault("Nitro", tmp_path / "all")  # no files at all, still refused
    with pytest.raises(TenantGuardError):
        prepare(tmp_path / "all", IngestOptions(tenant="pessoal", dry_run=True))


def test_prune_prefixes(tmp_path, make_vault):
    vault = make_vault("Pessoal")
    (vault / "Sub").mkdir()
    assert prune_prefixes(vault, None, discover(vault)) == ["obsidian://Pessoal/"]
    assert prune_prefixes(vault / "Sub", None, discover(vault / "Sub")) == ["obsidian://Pessoal/Sub/"]
    root = tmp_path / "mix"
    write(make_vault("Livros", root) / "l.md", "x")
    assert prune_prefixes(root, "lbl", discover(root)) == ["file://lbl/", "obsidian://Livros/"]
