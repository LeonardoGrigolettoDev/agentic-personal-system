from kb.chunker import Section, chunk_sections, estimate_tokens, split_blocks, split_markdown_sections


def sentences(n: int, word: str = "palavra") -> str:
    return " ".join(f"Frase {i} com {word} suficiente para encher o bloco de texto." for i in range(n))


def paragraphs(n: int, size: int = 4) -> str:
    return "\n\n".join(sentences(size, f"p{i}") for i in range(n))


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_heading_paths_breadcrumb():
    body = "intro\n\n# A\n\ntexto a\n\n## A1\n\ntexto a1\n\n### A1x\n\nx\n\n## A2\n\ntexto a2\n\n# B\n\ntexto b"
    sections = split_markdown_sections(body)
    assert [s.heading_path for s in sections] == [
        (), ("A",), ("A", "A1"), ("A", "A1", "A1x"), ("A", "A2"), ("B",),
    ]
    chunks = chunk_sections(sections, merge_below=0)
    assert [c.heading_path for c in chunks] == [None, "A", "A > A1", "A > A1 > A1x", "A > A2", "B"]


def test_small_sections_merge_forward_with_inline_heading():
    sections = split_markdown_sections("# Nota\n\ncurto\n\n## Sub\n\ntambém curto")
    (chunk,) = chunk_sections(sections)
    assert chunk.content == "curto\n\n## Sub\n\ntambém curto"
    assert chunk.heading_path == "Nota"  # common prefix of merged sections


def test_large_sections_split_on_headings():
    body = f"# A\n\n{paragraphs(3)}\n\n# B\n\n{paragraphs(3)}"
    chunks = chunk_sections(split_markdown_sections(body))
    assert [c.heading_path for c in chunks] == ["A", "B"]
    assert not chunks[1].content.startswith("# B")


def test_sizes_respect_max_and_overlap_is_carried():
    text = paragraphs(30)
    chunks = chunk_sections([Section(text=text)], max_tokens=500, overlap_tokens=50)
    assert len(chunks) > 3
    for c in chunks:
        assert c.token_count <= 500
        assert c.token_count == estimate_tokens(c.content)
    for c in chunks[:-1]:
        assert c.token_count >= 300  # packed close to the target, not tiny fragments
    for prev, nxt in zip(chunks, chunks[1:]):
        first_para = nxt.content.split("\n\n")[0]
        assert prev.content.endswith(first_para)  # overlap = tail of previous chunk
        assert 0 < estimate_tokens(first_para) <= 50


def test_overlap_disabled():
    chunks = chunk_sections([Section(text=paragraphs(30))], overlap_tokens=0)
    joined = "\n\n".join(c.content for c in chunks)
    assert joined == paragraphs(30)


def test_oversized_paragraph_split_by_sentences():
    chunks = chunk_sections([Section(text=sentences(200))])
    assert len(chunks) > 1
    assert all(c.token_count <= 500 for c in chunks)
    assert all(c.content.rstrip().endswith(".") for c in chunks)


def test_code_block_with_blank_lines_is_one_block():
    text = "antes\n\n```python\ndef f():\n\n\n    return 1\n```\n\ndepois"
    assert split_blocks(text) == ["antes", "```python\ndef f():\n\n\n    return 1\n```", "depois"]


def test_code_blocks_never_split_when_avoidable():
    code = "```python\n" + "\n".join(f"x{i} = {i}  # linha" for i in range(80)) + "\n```"  # ~1700 chars
    text = f"{paragraphs(3)}\n\n{code}\n\n{paragraphs(2)}"
    chunks = chunk_sections([Section(text=text)])
    holding = [c for c in chunks if "```python" in c.content]
    assert len(holding) == 1 and code in holding[0].content
    assert all(c.content.count("```") % 2 == 0 for c in chunks)


def test_huge_code_block_split_on_lines_and_refenced():
    code = "```sql\n" + "\n".join(f"SELECT {i} FROM tabela_{i};" for i in range(400)) + "\n```"
    chunks = chunk_sections([Section(text=code)])
    assert len(chunks) > 1
    for c in chunks:
        assert c.content.startswith("```sql\n") and c.content.endswith("\n```")
        assert c.token_count <= 500


def test_empty_sections_dropped_and_indexes_contiguous():
    sections = [Section(text="   \n\n"), Section(("H",), "", 1), Section(text="conteúdo")]
    chunks = chunk_sections(sections)
    assert [(c.index, c.content) for c in chunks] == [(0, "conteúdo")]


def test_page_metadata_carried():
    sections = [Section(text="um", metadata={"page": 1}), Section(text="dois", metadata={"page": 2}),
                Section(text=paragraphs(3), metadata={"page": 3})]
    chunks = chunk_sections(sections)
    # small pages merge forward, so the first chunk starts at page 1 and spans into later pages
    assert chunks[0].metadata["page"] == 1 and chunks[0].metadata["page_end"] >= 2
    last = chunks[-1].metadata
    assert last.get("page_end", last["page"]) == 3
    starts = [c.metadata["page"] for c in chunks]
    assert starts == sorted(starts)


def test_deterministic():
    body = f"# T\n\n{paragraphs(12)}\n\n## S\n\n```\ncode\n```\n\n{paragraphs(5)}"
    assert chunk_sections(split_markdown_sections(body)) == chunk_sections(split_markdown_sections(body))
