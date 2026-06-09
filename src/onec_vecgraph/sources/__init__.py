"""Pluggable corpora for multi-source vectorization (config / ITS / project artifacts).

Each Source adapter yields normalized DocUnits; the shared ingest pipeline chunks, embeds, writes
:Document/:Artifact owners + :Chunk, and links them to config Objects. See docs/STATE.md §11.1.
"""

from .base import DocUnit, Source, owner_fqn, sha1_text

__all__ = ["DocUnit", "Source", "owner_fqn", "sha1_text"]
