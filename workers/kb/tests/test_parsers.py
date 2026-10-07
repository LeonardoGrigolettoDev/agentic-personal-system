from kb.parsers import clean_subtitles, normalize, parse_file, render_csv

SRT = """1
00:00:00,000 --> 00:00:02,500
Olá, bem-vindos à aula.

2
00:00:02,500 --> 00:00:05,000
<i>Hoje</i> vamos falar de Go.

3
00:00:05,000 --> 00:00:06,000
Hoje vamos falar de Go.

10
00:01:00,000 --> 00:01:02,000
Temos 10 exemplos.
"""

VTT = """WEBVTT
Kind: captions

NOTE isto é uma nota
que continua

00:00.000 --> 00:02.000 align:start
<v Ana>Primeira fala</v>

00:02.000 --> 00:04.000
{\\an8}Segunda fala
"""


def test_srt_strips_numbers_timestamps_tags_and_repeats():
    text = clean_subtitles(SRT)
    assert text == "Olá, bem-vindos à aula. Hoje vamos falar de Go. Temos 10 exemplos."
    assert "-->" not in text


def test_vtt_strips_header_notes_and_voice_tags():
    assert clean_subtitles(VTT) == "Primeira fala Segunda fala"


def test_transcript_file_parsing(tmp_path):
    p = tmp_path / "aula.srt"
    p.write_text(SRT, encoding="utf-8")
    doc = parse_file(p)
    assert doc.kind == "transcript" and doc.title == "aula"
    assert doc.mime_type == "application/x-subrip"


def test_csv_renders_header_and_truncates():
    text = "nome;valor\n" + "".join(f"item{i};{i}\n" for i in range(120))
    rendered, meta = render_csv(text, max_rows=5)
    lines = rendered.splitlines()
    assert lines[0] == "nome | valor"
    assert lines[1:6] == [f"item{i} | {i}" for i in range(5)]
    assert "mostrando 5 de 120" in lines[-1]
    assert meta == {"columns": ["nome", "valor"], "rows": 120, "truncated": True}


def test_normalize_collapses_blank_lines_outside_code_only():
    text = "a  \r\n\r\n\r\n\r\nb\n```\nx\n\n\n\ny\n```\n"
    assert normalize(text) == "a\n\nb\n```\nx\n\n\n\ny\n```"


def test_text_file_latin1_fallback(tmp_path):
    p = tmp_path / "velho.txt"
    p.write_bytes("ação".encode("cp1252"))
    assert parse_file(p).content == "ação"


def _minimal_pdf(pages: list[str]) -> bytes:
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_pages_become_sections_with_page_numbers(tmp_path):
    p = tmp_path / "manual.pdf"
    p.write_bytes(_minimal_pdf(["Primeira pagina", "Segunda pagina"]))
    doc = parse_file(p)
    assert doc.kind == "pdf" and doc.metadata["pages"] == 2
    assert [s.metadata["page"] for s in doc.sections] == [1, 2]
    assert "Primeira pagina" in doc.sections[0].text and "Segunda pagina" in doc.sections[1].text
