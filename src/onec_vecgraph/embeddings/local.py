"""Local embedding provider (sentence-transformers).

Default model is Qwen3-Embedding (1C-RAG best-practice family). Uses CUDA when
available (RTX 50xx / Blackwell needs the cu128 torch build, see pyproject).
"""

from __future__ import annotations

from typing import Sequence

from .base import EmbeddingProvider


class LocalEmbeddingProvider(EmbeddingProvider):
    name = "local"

    def __init__(
        self,
        model_name: str,
        device: str = "auto",
        query_prompt: str = "query",
        batch_size: int = 16,
        max_seq_length: int = 256,
    ) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.query_prompt = query_prompt
        self.batch_size = batch_size
        self._model = SentenceTransformer(model_name, device=device)
        # Cap sequence length: bounds VRAM (attention is O(seq^2)) and speeds up encoding.
        if max_seq_length:
            self._model.max_seq_length = max_seq_length
        # get_sentence_embedding_dimension was renamed to get_embedding_dimension.
        dim_fn = getattr(self._model, "get_embedding_dimension", None) or \
            self._model.get_sentence_embedding_dimension
        self.dim = int(dim_fn())

    def embed(self, texts: Sequence[str], is_query: bool = False) -> list[list[float]]:
        kwargs = {
            "batch_size": self.batch_size,
            "normalize_embeddings": True,
            "convert_to_numpy": True,
            "show_progress_bar": False,
        }
        items = list(texts)
        if is_query and self.query_prompt:
            try:
                vecs = self._model.encode(items, prompt_name=self.query_prompt, **kwargs)
            except (ValueError, KeyError):
                vecs = self._model.encode(items, **kwargs)
        else:
            vecs = self._model.encode(items, **kwargs)
        return [v.tolist() for v in vecs]
