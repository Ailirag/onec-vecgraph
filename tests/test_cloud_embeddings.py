"""Cloud provider tests that do NOT require the openai/voyageai packages or network:
exercise the pure helpers and the batching/normalization in embed() via a fake _raw_embed."""
import math

from onec_vecgraph.config import Settings
from onec_vecgraph.embeddings.cloud import CloudEmbeddingProvider, _l2


def test_empty_env_strings_coerced_to_none_but_real_values_kept() -> None:
    s = Settings(embedding_dimensions="", openai_base_url="", openai_api_key="  ")
    assert s.embedding_dimensions is None and s.openai_base_url is None and s.openai_api_key is None
    assert Settings(embedding_dimensions="1024").embedding_dimensions == 1024


def test_l2_normalizes_to_unit_length() -> None:
    v = _l2([3.0, 4.0])
    assert math.isclose(v[0], 0.6) and math.isclose(v[1], 0.8)
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0)


def _fake_provider():
    # Bypass __init__ (which would import openai/voyageai); wire just what embed() needs.
    p = object.__new__(CloudEmbeddingProvider)
    p._kind = "openai"
    p.model = "text-embedding-3-small"
    p._override_dim = None
    p.calls = []

    def fake_raw(texts, input_type):
        p.calls.append((len(texts), input_type))
        return [[3.0, 4.0] for _ in texts]

    p._raw_embed = fake_raw
    return p


def test_embed_normalizes_and_preserves_order_and_count() -> None:
    p = _fake_provider()
    out = p.embed(["a", "b", "c"], is_query=True)
    assert len(out) == 3
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0) for v in out)
    assert p.calls == [(3, "query")]  # is_query → input_type 'query'


def test_embed_batches_in_chunks_of_128() -> None:
    p = _fake_provider()
    out = p.embed(["x"] * 300, is_query=False)
    assert len(out) == 300
    assert [n for n, _ in p.calls] == [128, 128, 44]
    assert all(it == "document" for _, it in p.calls)
