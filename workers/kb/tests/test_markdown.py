from kb.parsers import parse_markdown_text, split_frontmatter

NOTE = """---
title: Plano de estudos
tags: [aprendizado, "#go"]
aliases: Estudos
created: 2026-01-15
rating: 4
---
# Cabeçalho

Revisar [[Concorrência em Go|goroutines]] e [[Canais#Buffer]] hoje. #foco #2026 #área/estudo
Veja também [[Notas Diárias]] e [[Canais]].
![[diagrama.png]]
Texto antes ![[outro.pdf]] depois.
Link markdown [x](#ancora) e url http://exemplo.com/#frag não são tags.
Código inline `#naotag` ignorado. %%comentário oculto%%

```bash
# isto não é heading nem #tag
echo [[nao-link]]
```

## Seção dois

Mais texto.
"""


def test_frontmatter_metadata():
    doc = parse_markdown_text(NOTE, "fallback")
    assert doc.title == "Plano de estudos"
    assert doc.metadata["aliases"] == ["Estudos"]
    assert doc.metadata["created"] == "2026-01-15"
    assert doc.metadata["frontmatter"]["rating"] == 4
    assert doc.mime_type == "text/markdown"


def test_tags_from_frontmatter_and_inline():
    doc = parse_markdown_text(NOTE, "x")
    assert doc.metadata["tags"] == ["aprendizado", "go", "foco", "área/estudo"]


def test_wikilinks_captured_and_rendered_as_text():
    doc = parse_markdown_text(NOTE, "x")
    assert doc.metadata["links"] == ["Concorrência em Go", "Canais", "Notas Diárias"]
    assert "Revisar goroutines e Canais > Buffer hoje." in doc.content
    assert "Veja também Notas Diárias e Canais." in doc.content
    assert "[[" not in doc.content.split("```")[0]


def test_embeds_removed_and_recorded():
    doc = parse_markdown_text(NOTE, "x")
    assert "diagrama.png" not in doc.content
    assert "Texto antes  depois." in doc.content
    assert doc.metadata["embeds"] == ["diagrama.png", "outro.pdf"]


def test_code_blocks_untouched_and_comments_dropped():
    doc = parse_markdown_text(NOTE, "x")
    assert "echo [[nao-link]]" in doc.content
    assert "# isto não é heading nem #tag" in doc.content
    assert "comentário oculto" not in doc.content


def test_sections_follow_headings_not_code():
    doc = parse_markdown_text(NOTE, "x")
    paths = [s.heading_path for s in doc.sections]
    assert paths == [("Cabeçalho",), ("Cabeçalho", "Seção dois")]


def test_title_falls_back_to_h1_then_filename():
    assert parse_markdown_text("# Título H1\n\ntexto", "arquivo").title == "Título H1"
    assert parse_markdown_text("só texto", "arquivo").title == "arquivo"


def test_invalid_frontmatter_is_ignored():
    meta, body = split_frontmatter("---\n: [broken\n---\ncorpo")
    assert meta == {} and body == "corpo"


def test_no_frontmatter_when_not_at_start():
    doc = parse_markdown_text("texto\n---\na: 1\n---\n", "x")
    assert "frontmatter" not in doc.metadata
