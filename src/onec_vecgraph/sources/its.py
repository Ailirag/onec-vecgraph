"""ITS corpus adapter: consumes the normalized output of the external ITS parser.

Reads JSON units (one object, or a list, per file) per docs/ITS_PARSER_REQUIREMENTS.md from a git
repo or local dir. The hard ITS parsing lives in that external tool; this adapter just normalizes.
"""

from __future__ import annotations

import json
from typing import Iterator

from .base import DocUnit, Source, sha1_text
from .git_repo import iter_files, materialize


class ItsSource(Source):
    name = "its"
    source = "its"
    owner_label = "Document"

    def __init__(self, entry: dict) -> None:
        self.entry = entry
        self.globs = entry.get("globs") or ["**/*.json"]
        # Manifest-level defaults for the classification facets (a unit may override per-record).
        self.default_topic = entry.get("doc_topic") or "config"  # ITS docs are usually about a config
        self.corpus_version = entry.get("corpus_version")  # e.g. 'config:ERP_2.5.18'

    def units(self) -> Iterator[DocUnit]:
        root = materialize(self.entry)
        for f in iter_files(root, self.globs):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for d in (data if isinstance(data, list) else [data]):
                text = (d.get("text") or "").strip()
                if not text:
                    continue
                ext_id = str(d.get("id") or sha1_text(d.get("source_url") or "", text)[:16])
                yield DocUnit(
                    external_id=ext_id,
                    title=d.get("title") or ext_id,
                    text=text,
                    version_hash=d.get("version_hash") or sha1_text(text),
                    section_path=list(d.get("section_path") or []),
                    links=list(d.get("related_fqns") or []),
                    source_url=d.get("source_url"),
                    extra={"related_kinds": d.get("related_kinds"), "lang": d.get("lang"),
                           "product": d.get("product"),
                           "doc_topic": d.get("doc_topic") or self.default_topic,
                           "corpus_version": d.get("corpus_version") or self.corpus_version},
                )
