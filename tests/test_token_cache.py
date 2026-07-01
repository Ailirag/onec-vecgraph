import hashlib

import pytest

from onec_vecgraph import token_store
from onec_vecgraph.config import Settings


class _FakeStore:
    def __init__(self, mapping: dict[str, dict]):
        self.mapping = mapping  # token_hash -> record
        self.calls = 0

    def resolve_token(self, token_hash: str):
        self.calls += 1
        return self.mapping.get(token_hash)


@pytest.fixture(autouse=True)
def _clean():
    token_store.invalidate()
    yield
    token_store.invalidate()


def _install(monkeypatch, store, clock=None):
    monkeypatch.setattr(token_store, "_get_store", lambda settings: store)
    if clock is not None:
        monkeypatch.setattr(token_store.time, "monotonic", clock)


def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def test_positive_lookup_is_cached(monkeypatch) -> None:
    store = _FakeStore({_h("t"): {"tenant_id": "acme", "config_id": None}})
    _install(monkeypatch, store)
    s = Settings(token_cache_ttl_seconds=30)
    assert token_store.lookup_token("t", s)["tenant_id"] == "acme"
    assert token_store.lookup_token("t", s)["tenant_id"] == "acme"
    assert store.calls == 1  # second call served from cache


def test_negative_lookup_is_cached(monkeypatch) -> None:
    store = _FakeStore({})
    _install(monkeypatch, store)
    s = Settings(token_cache_ttl_seconds=30)
    assert token_store.lookup_token("nope", s) is None
    assert token_store.lookup_token("nope", s) is None
    assert store.calls == 1


def test_invalidate_forces_refetch(monkeypatch) -> None:
    store = _FakeStore({_h("t"): {"tenant_id": "acme", "config_id": None}})
    _install(monkeypatch, store)
    s = Settings(token_cache_ttl_seconds=30)
    token_store.lookup_token("t", s)
    token_store.invalidate()
    token_store.lookup_token("t", s)
    assert store.calls == 2


def test_ttl_expiry_refetches(monkeypatch) -> None:
    store = _FakeStore({_h("t"): {"tenant_id": "acme", "config_id": None}})
    now = {"v": 1000.0}
    _install(monkeypatch, store, clock=lambda: now["v"])
    s = Settings(token_cache_ttl_seconds=30)
    token_store.lookup_token("t", s)
    now["v"] += 31  # past TTL
    token_store.lookup_token("t", s)
    assert store.calls == 2


def test_empty_token_returns_none_without_store(monkeypatch) -> None:
    store = _FakeStore({})
    _install(monkeypatch, store)
    assert token_store.lookup_token("", Settings()) is None
    assert store.calls == 0
