"""Project-artifact corpus adapter: Markdown/AsciiDoc docs from a git repository (or local dir).

Each file is split into sections by headings; each section becomes a DocUnit. version_hash is the
content hash, so only changed sections re-embed on the next ingest.
"""

from __future__ import annotations

from typing import Iterator

from .base import DocUnit, Source, sha1_text
from .git_repo import iter_files, materialize
from .markdown import split_markdown_sections


class GitArtifactsSource(Source):
    name = "git_artifacts"
    source = "artifact"
    owner_label = "Artifact"

    def __init__(self, entry: dict) -> None:
        self.entry = entry
        self.globs = entry.get("globs") or ["**/*.md", "**/*.markdown", "**/*.adoc"]
        # Classification facets (manifest-level): project/task docs by default.
        self.doc_topic = entry.get("doc_topic") or "task"
        self.corpus_version = entry.get("corpus_version")  # e.g. 'task:JIRA-1234' / 'git:<tag>'

    def units(self) -> Iterator[DocUnit]:
        root = materialize(self.entry)
        for f in iter_files(root, self.globs):
            rel = f.relative_to(root).as_posix()
            text = f.read_text(encoding="utf-8", errors="replace")
            for idx, sec in enumerate(split_markdown_sections(text)):
                body = sec["body"].strip()
                if not body:
                    continue
                title = sec["title"] or rel
                yield DocUnit(
                    external_id=f"{rel}#{idx}",
                    title=title,
                    text=body,
                    version_hash=sha1_text(rel, sec["title"], body),
                    section_path=[rel, *sec["path"]],
                    source_url=rel,
                    extra={"doc_topic": self.doc_topic, "corpus_version": self.corpus_version},
                )
