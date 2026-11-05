"""Dedicated, tenant-scoped search over official configuration documentation from 1C:ITS."""

from __future__ import annotations

import inspect
from unittest import mock

from onec_vecgraph import server as vg_server
from onec_vecgraph.sources.its import ItsSource


def test_corpus_tag_comes_from_the_adapter() -> None:
    assert vg_server.ITS_CORPUS == ItsSource.source == "its"


def test_search_is_pinned_to_configuration_its() -> None:
    captured = {}

    def fake_hybrid(store, tenant, query, embedder, top_k, **kwargs):
        captured.update(kwargs)
        captured.update(tenant=tenant, query=query, top_k=top_k)
        return {"results": []}

    with mock.patch.object(vg_server.queries, "hybrid_search", fake_hybrid), \
         mock.patch.object(vg_server, "Neo4jStore") as store_cls, \
         mock.patch("onec_vecgraph.embeddings.runtime.provider", return_value=object()), \
         mock.patch("onec_vecgraph.embeddings.runtime.reranker", return_value=None), \
         mock.patch.object(vg_server, "_tenant", return_value="acme_ut"), \
         mock.patch.object(vg_server, "_shared", return_value="__shared__"):
        store_cls.from_settings.return_value.__enter__.return_value = object()
        vg_server.its_config_search(None, "закупка импортных товаров", top_k=6)

    assert captured["tenant"] == "acme_ut"
    assert captured["source"] == ["its"]
    assert captured["doc_topic"] == "config"
    assert captured["shared_tenant_id"] == "__shared__"
    assert captured["top_k"] == 6


def test_caller_cannot_override_scope() -> None:
    params = set(inspect.signature(vg_server.its_config_search).parameters)
    assert "tenant_id" not in params
    assert "source" not in params
    assert "doc_topic" not in params
    assert "corpus_version" not in params


def test_description_states_snapshot_and_standard_baseline() -> None:
    text = (vg_server.its_config_search.__doc__ or "").lower()
    assert "snapshot" in text
    assert "standard baseline" in text
    assert "onec-lite" in text



class _FakeStore:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def read(self, cypher, **params):
        self.calls.append((cypher, params))
        return self.rows


def test_scoped_get_document_rejects_non_config_edition() -> None:
    import pytest

    from onec_vecgraph import queries

    store = _FakeStore([])
    for bad in [dict(source="its", corpus_version="platform:v8std"),
                dict(source="artifact", corpus_version="config:UT_11.5"),
                dict(source="its", corpus_version="config:")]:
        with pytest.raises(ValueError):
            queries.get_document(store, "acme_ut", "its:a", shared_tenant_id="__shared__", **bad)
    assert store.calls == []


def test_scoped_get_document_reads_caller_tenant_only() -> None:
    from onec_vecgraph import queries

    store = _FakeStore([{"props": {"fqn": "its:a", "source": "its", "tenant_id": "acme_ut",
                                   "corpus_version": "config:UT_11.5", "doc_topic": "config"},
                         "texts": ["one", "two"], "links": []}])
    out = queries.get_document(store, "acme_ut", "its:a", shared_tenant_id="__shared__",
                               source="its", corpus_version="config:UT_11.5")
    (cypher, params), = store.calls
    assert params["t"] == "acme_ut" and "__shared__" not in params.values()
    assert out["found"] and out["text"] == "one\n\ntwo"
    assert out["read_protocol"] == "its-owner-scoped-v1"

    store.rows = []
    assert queries.get_document(store, "acme_ut", "its:x", source="its",
                                corpus_version="config:UT_11.5")["found"] is False
