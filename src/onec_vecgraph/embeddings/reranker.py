"""Optional cross-encoder reranker (e.g. bge-reranker-v2-m3).

Off by default (RERANK_ENABLED). Re-scores (query, chunk-text) pairs to refine the
top hybrid candidates. Requires the 'local-embeddings' extra.
"""

from __future__ import annotations

from typing import Sequence


class RerankProvider:
    def __init__(self, model_name: str, device: str = "auto") -> None:
        import torch
        from sentence_transformers import CrossEncoder

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self._model = CrossEncoder(model_name, device=device)

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        return [float(s) for s in self._model.predict(list(pairs))]
