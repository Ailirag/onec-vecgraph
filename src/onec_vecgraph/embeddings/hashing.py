"""Dependency-free, deterministic embeddings for development and tests.

NOT for production retrieval quality — it is a hashed bag-of-tokens used so the full
pipeline (chunk -> embed -> store -> search) is exercisable without downloading models.
Swap to the `local` or `openai` provider for real search.
"""

from __future__ import annotations

import hashlib
import math
from typing import Sequence

from .base import EmbeddingProvider


class HashingEmbeddingProvider(EmbeddingProvider):
    name = "hashing"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str], is_query: bool = False) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest, "big") % self.dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
