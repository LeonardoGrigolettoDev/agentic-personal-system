"""Streaming multipart upload straight into storage://media/input/<uuid>.<ext>.

The body is parsed incrementally (python-multipart) and the `file` part is written to `<name>.part`
as it arrives, so a 2 GB upload never sits in RAM or in a temp spool, and the size limit aborts the
transfer as soon as it is crossed.
"""

import errno
import hashlib
import logging
import uuid
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from edge import media
from edge.errors import BadRequest, EdgeError, TooLarge, UnsupportedMedia
from edge.storage import LocalStorage, make_uri

log = logging.getLogger(__name__)

FILE_FIELD = b"file"
MAX_OTHER_FIELDS_BYTES = 64 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # boundaries + part headers allowed on top of the file itself
# Enforced by the parser itself before our header callbacks see any byte (python-multipart >= 0.0.27; passing
# them explicitly fails loudly on an older version instead of silently accepting unbounded part headers).
MAX_PART_HEADERS = 8
MAX_PART_HEADER_BYTES = 4096


class _Sink:
    """python-multipart callbacks: route the `file` part to disk, ignore (bounded) other fields."""

    def __init__(self, storage: LocalStorage, max_bytes: int) -> None:
        self.storage = storage
        self.max_bytes = max_bytes
        self.headers: dict[bytes, bytes] = {}
        self._field, self._value = b"", b""
        self.in_file = False
        self.fh: BinaryIO | None = None
        self.tmp: Path | None = None
        self.final: Path | None = None
        self.sha = hashlib.sha256()
        self.size = 0
        self.other_bytes = 0
        self.result: dict | None = None

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }

    def on_part_begin(self) -> None:
        self.headers, self.in_file = {}, False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        self.headers[self._field.lower()] = self._value
        self._field, self._value = b"", b""

    def on_headers_finished(self) -> None:
        _, params = parse_options_header(self.headers.get(b"content-disposition", b""))
        if params.get(b"name") != FILE_FIELD:
            return
        if self.result is not None or self.fh is not None:
            raise BadRequest("envie apenas um arquivo no campo 'file'")
        raw_name = params.get(b"filename", b"").decode("utf-8", errors="replace")
        filename = PurePosixPath(raw_name.replace("\\", "/")).name
        ext = media.extension(filename)
        if ext not in media.ALLOWED_EXTS:
            allowed = ", ".join(sorted(media.ALLOWED_EXTS))
            raise UnsupportedMedia(f"extensão {('.' + ext) if ext else '(nenhuma)'} não permitida; use: {allowed}")
        file_id = uuid.uuid4().hex
        uri = make_uri("media", f"input/{file_id}.{ext}")
        self.final = self.storage.path(uri)
        self.final.parent.mkdir(parents=True, exist_ok=True)
        self.tmp = self.final.with_name(self.final.name + ".part")
        self.fh = self.tmp.open("wb", buffering=1024 * 1024)
        self.in_file = True
        self.result = {"id": file_id, "uri": uri, "filename": filename, "ext": ext, "kind": media.media_kind(filename)}

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        n = end - start
        if not self.in_file:
            self.other_bytes += n
            if self.other_bytes > MAX_OTHER_FIELDS_BYTES:
                raise TooLarge("campos de formulário grandes demais")
            return
        self.size += n
        if self.size > self.max_bytes:
            raise TooLarge(f"arquivo maior que o limite de {self.max_bytes // (1024 * 1024)} MB (EDGE_MAX_UPLOAD_MB)")
        chunk = data[start:end]
        self.fh.write(chunk)
        self.sha.update(chunk)

    def on_part_end(self) -> None:
        if not self.in_file:
            return
        self.fh.close()
        self.fh = None
        self.in_file = False
        if self.size == 0:
            raise BadRequest("arquivo vazio")
        self.tmp.replace(self.final)
        self.result |= {"bytes": self.size, "sha256": self.sha.hexdigest()}

    def abort(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None
        for p in (self.tmp, self.final):
            if p is not None:
                p.unlink(missing_ok=True)


async def receive_upload(request: Request, storage: LocalStorage, max_bytes: int) -> dict:
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise UnsupportedMedia("use multipart/form-data com o arquivo no campo 'file'")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes + MULTIPART_OVERHEAD:
        raise TooLarge(f"arquivo maior que o limite de {max_bytes // (1024 * 1024)} MB (EDGE_MAX_UPLOAD_MB)")

    sink = _Sink(storage, max_bytes)
    try:
        parser = MultipartParser(
            boundary,
            sink.callbacks(),
            max_header_count=MAX_PART_HEADERS,
            max_header_size=MAX_PART_HEADER_BYTES,
        )
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
        if sink.result is None or "bytes" not in sink.result:
            raise BadRequest("campo 'file' ausente ou incompleto no multipart")
    except (FormParserError, ValueError) as exc:
        sink.abort()
        raise BadRequest(f"multipart inválido: {exc}") from None
    except OSError as exc:
        sink.abort()
        if exc.errno == errno.ENOSPC:
            raise EdgeError("sem espaço em disco para o upload", status=507) from None
        log.error("upload write failed", extra={"error": str(exc)})
        raise EdgeError("falha ao gravar o upload", status=500) from None
    except BaseException as exc:
        sink.abort()
        if isinstance(exc, ClientDisconnect):
            log.info("upload aborted by client", extra={"bytes": sink.size})
        raise
    log.info("uploaded", extra={"uri": sink.result["uri"], "bytes": sink.size, "ext": sink.result["ext"]})
    return sink.result
