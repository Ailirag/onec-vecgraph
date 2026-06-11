"""Clean-room reader for 1C v8 help containers (.hbk).

Validated against real shcntx_ru.hbk (8.3.27.1989). Format (refs: v8unpack / onec-dtools /
MIT onec-help-mcp):
  - 16-byte FileHeader; the top-level element/directory table is a block chain at offset 16.
  - Each block has a 31-byte text header b'\\r\\n%08x %08x %08x \\r\\n' = (doc_len, block_len, next_addr),
    chained via next_addr (0x7fffffff = end); doc_len in the first block = total document length.
  - Directory = 12-byte records <III> = (attr_block_addr, data_block_addr, 0x7fffffff).
  - Top-level elements are named (Book, FileStorage, MainData, ...). The help HTML pages live in the
    `FileStorage` element, which is a plain ZIP archive (read with stdlib zipfile).
Only the stdlib is used (struct, zipfile, io).
"""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path
from typing import Iterator

_END = 0x7FFFFFFF
_BH = 31  # block header size


def _block_header(buf: bytes, pos: int) -> tuple[int, int, int]:
    h = buf[pos:pos + _BH]
    if len(h) < _BH or h[:2] != b"\r\n" or h[-2:] != b"\r\n":
        raise ValueError(f"bad v8 block header at offset {pos}")
    return int(h[2:10], 16), int(h[11:19], 16), int(h[20:28], 16)


def _read_document(buf: bytes, addr: int) -> bytes:
    """Concatenate a document's block chain from `addr`, truncated to its declared length."""
    doc_len, _, _ = _block_header(buf, addr)
    out = bytearray()
    pos = addr
    while True:
        _, block_len, next_addr = _block_header(buf, pos)
        start = pos + _BH
        out += buf[start:start + block_len]
        if next_addr == _END or next_addr == 0 or next_addr >= len(buf):
            break
        pos = next_addr
    return bytes(out[:doc_len]) if doc_len else bytes(out)


def _name_from_attr(attr: bytes) -> str:
    # 8b creation + 8b modified + 4b flags = 20-byte head, then UTF-16LE name
    tail = attr[20:] if len(attr) > 20 else attr
    return tail.decode("utf-16-le", "replace").rstrip("\x00").strip()


def named_elements(path: str | Path) -> dict[str, bytes]:
    """Top-level named elements of the v8 container: {name: raw_document_bytes}."""
    buf = Path(path).read_bytes()
    directory = _read_document(buf, 16)
    elems: dict[str, bytes] = {}
    for i in range(0, len(directory) - 11, 12):
        attr_addr, data_addr, _ = struct.unpack_from("<III", directory, i)
        if attr_addr >= len(buf) or data_addr >= len(buf):
            break
        name = _name_from_attr(_read_document(buf, attr_addr))
        elems[name] = _read_document(buf, data_addr)
    return elems


def iter_html_pages(path: str | Path) -> Iterator[tuple[str, bytes]]:
    """Yield (zip_path, html_bytes) for every HTML help page in the container's FileStorage ZIP."""
    fs = named_elements(path).get("FileStorage")
    if not fs or fs[:4] != b"PK\x03\x04":
        return
    with zipfile.ZipFile(io.BytesIO(fs)) as z:
        for name in z.namelist():
            if name.lower().endswith((".html", ".htm")):
                yield name, z.read(name)
