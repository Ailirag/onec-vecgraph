"""Source adapter contract for multi-source vectorization."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator


def sha1_text(*parts: str) -> str:
    """Stable content hash (used as version_hash when an adapter has no native version)."""
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def owner_fqn(source: str, external_id: str) -> str:
    """Owner node fqn for a doc unit: '<source>:<external_id>' (e.g. 'its:art-123')."""
    return f"{source}:{external_id}"


@dataclass
class DocUnit:
    """One normalized, logically-granular document section from a source."""

    external_id: str  # stable id within the source (drives owner fqn + dedup)
    title: str
    text: str
    version_hash: str  # changes only when content changes → incremental re-ingest
    section_path: list[str] = field(default_factory=list)  # breadcrumbs for context prefix
    links: list[str] = field(default_factory=list)  # explicit config fqns mentioned → MENTIONS
    source_url: str | None = None
    extra: dict = field(default_factory=dict)  # e.g. related_kinds, lang, product


class Source(ABC):
    """A corpus adapter. Subclasses set `source` (corpus tag) and `owner_label`, and yield DocUnits."""

    name: str  # adapter type, e.g. 'its' | 'git_artifacts'
    source: str  # corpus tag stored on chunks/owners, e.g. 'its' | 'artifact'
    owner_label: str  # 'Document' | 'Artifact'

    @abstractmethod
    def units(self) -> Iterator[DocUnit]:
        """Yield normalized document units for this corpus."""
        raise NotImplementedError
