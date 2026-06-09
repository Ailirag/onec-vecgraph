"""Map a manifest source entry to a Source adapter."""

from __future__ import annotations

from .base import Source

# Doc-corpus source types handled by the ingest pipeline. `config_dump` is NOT here — it keeps its
# rich typed pipeline (index + vectorize) and is delegated by the orchestrator.
DOC_SOURCE_TYPES = ("its", "git_artifacts")


def build_source(entry: dict) -> Source:
    """Construct a doc Source from a manifest entry ({type, repo|path, branch?, globs?})."""
    t = entry.get("type")
    if t == "its":
        from .its import ItsSource

        return ItsSource(entry)
    if t == "git_artifacts":
        from .git_artifacts import GitArtifactsSource

        return GitArtifactsSource(entry)
    raise ValueError(f"Unknown / non-doc source type: {t!r} (doc types: {DOC_SOURCE_TYPES})")
