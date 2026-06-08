"""Process-wide embedding provider cache (loading a model is expensive)."""

from __future__ import annotations

from ..config import Settings
from .base import EmbeddingProvider, get_provider

_PROVIDER: EmbeddingProvider | None = None
_KEY: tuple | None = None


def provider(settings: Settings) -> EmbeddingProvider:
    global _PROVIDER, _KEY
    key = (settings.embedding_provider, settings.embedding_model, settings.embedding_device)
    if _PROVIDER is None or _KEY != key:
        _PROVIDER = get_provider(settings)
        _KEY = key
    return _PROVIDER


_RERANKER = None


def reranker(settings: Settings):
    """Cached cross-encoder reranker, or None if disabled."""
    global _RERANKER
    if not settings.rerank_enabled:
        return None
    if _RERANKER is None:
        from .reranker import RerankProvider

        _RERANKER = RerankProvider(settings.rerank_model, settings.embedding_device)
    return _RERANKER
