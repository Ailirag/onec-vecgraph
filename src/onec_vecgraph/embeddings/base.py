"""Embedding provider abstraction.

The provider is selected via settings so local (Qwen3-Embedding / BGE-m3) and cloud
(OpenAI / Voyage) backends are interchangeable. Concrete heavy providers are imported
lazily so M0 runs without ML dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ..config import Settings


class EmbeddingProvider(ABC):
    name: str
    dim: int

    @abstractmethod
    def embed(self, texts: Sequence[str], is_query: bool = False) -> list[list[float]]:
        """Return one embedding vector per input text.

        is_query lets providers apply a query-side instruction/prompt (e.g. Qwen3).
        """
        raise NotImplementedError


def get_provider(settings: Settings) -> EmbeddingProvider:
    provider = settings.embedding_provider.lower()
    if provider == "hashing":
        from .hashing import HashingEmbeddingProvider

        return HashingEmbeddingProvider(settings.embedding_dim)
    if provider == "local":
        from .local import LocalEmbeddingProvider  # requires: --extra local-embeddings

        return LocalEmbeddingProvider(
            settings.embedding_model,
            settings.embedding_device,
            settings.embedding_query_prompt,
            settings.embedding_batch_size,
            settings.embedding_max_seq_length,
        )
    if provider in ("openai", "voyage"):
        from .cloud import CloudEmbeddingProvider  # requires: --extra cloud-embeddings

        return CloudEmbeddingProvider(provider, settings)
    raise ValueError(f"Unknown embedding provider: {settings.embedding_provider!r}")
