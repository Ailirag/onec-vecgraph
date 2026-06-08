"""Cloud embedding providers (OpenAI / OpenAI-compatible / Voyage).

Selected via EMBEDDING_PROVIDER=openai|voyage; the model is EMBEDDING_MODEL (a cloud model
name, e.g. 'text-embedding-3-large' or 'voyage-3'). Vectors are L2-normalized (the Neo4j index
is cosine). Dimensions are taken from a known-model map, an explicit EMBEDDING_DIMENSIONS
override, or a one-shot probe call. Requires:  uv sync --extra cloud-embeddings.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..config import Settings
from .base import EmbeddingProvider

# Default output dimensions per model (avoids a probe call when known). ≤ 4096 (Neo4j limit).
_OPENAI_DIM = {
    "text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-ada-002": 1536,
}
_VOYAGE_DIM = {
    "voyage-3": 1024, "voyage-3-large": 1024, "voyage-3.5": 1024, "voyage-3.5-lite": 1024,
    "voyage-3-lite": 512, "voyage-large-2": 1536, "voyage-2": 1024, "voyage-code-3": 1024,
    "voyage-code-2": 1536,
}
_BATCH = 128  # inputs per API call (Voyage hard cap is 128; OpenAI handles more)


def _l2(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class CloudEmbeddingProvider(EmbeddingProvider):
    """OpenAI / OpenAI-compatible / Voyage embeddings behind the common provider interface."""

    def __init__(self, provider: str, settings: Settings) -> None:
        self.name = provider
        self.device = provider  # informational (shown in vectorize summary)
        self.model = settings.embedding_model
        self._override_dim = settings.embedding_dimensions

        if provider == "openai":
            if not settings.openai_api_key:
                raise ValueError("OPENAI_API_KEY is required for EMBEDDING_PROVIDER=openai")
            from openai import OpenAI

            self._client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
            self._kind = "openai"
            base_dim = _OPENAI_DIM.get(self.model)
        elif provider == "voyage":
            if not settings.voyage_api_key:
                raise ValueError("VOYAGE_API_KEY is required for EMBEDDING_PROVIDER=voyage")
            import voyageai

            self._client = voyageai.Client(api_key=settings.voyage_api_key)
            self._kind = "voyage"
            base_dim = _VOYAGE_DIM.get(self.model)
        else:  # pragma: no cover - guarded by get_provider
            raise ValueError(f"Unsupported cloud provider: {provider!r}")

        self.dim = self._override_dim or base_dim or len(self._raw_embed(["проба"], "query")[0])
        if self.dim > 4096:
            raise ValueError(f"Embedding dim {self.dim} exceeds Neo4j's 4096 limit (model {self.model!r}).")

    def _raw_embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if self._kind == "openai":
            kwargs = {"model": self.model, "input": texts}
            # 'dimensions' is supported only by text-embedding-3-* models.
            if self._override_dim and self.model.startswith("text-embedding-3"):
                kwargs["dimensions"] = self._override_dim
            resp = self._client.embeddings.create(**kwargs)
            return [d.embedding for d in resp.data]
        # voyage
        kwargs = {"model": self.model, "input_type": input_type}
        if self._override_dim:
            kwargs["output_dimension"] = self._override_dim
        return self._client.embed(texts, **kwargs).embeddings

    def embed(self, texts: Sequence[str], is_query: bool = False) -> list[list[float]]:
        input_type = "query" if is_query else "document"
        items = list(texts)
        out: list[list[float]] = []
        for start in range(0, len(items), _BATCH):
            batch = items[start : start + _BATCH]
            out.extend(_l2(v) for v in self._raw_embed(batch, input_type))
        return out
