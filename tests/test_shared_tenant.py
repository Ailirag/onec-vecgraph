from onec_vecgraph.config import Settings
from onec_vecgraph.storage.neo4j_store import Neo4jStore


def test_settings_search_scope_appends_shared() -> None:
    s = Settings(shared_tenant_id="__shared__", include_shared_tenant=True)
    assert s.search_scope("acme") == ["acme", "__shared__"]


def test_settings_search_scope_no_dup_when_caller_is_shared() -> None:
    s = Settings(shared_tenant_id="__shared__", include_shared_tenant=True)
    assert s.search_scope("__shared__") == ["__shared__"]


def test_settings_search_scope_disabled() -> None:
    s = Settings(shared_tenant_id="__shared__", include_shared_tenant=False)
    assert s.search_scope("acme") == ["acme"]


def test_store_scope_static_matches() -> None:
    assert Neo4jStore._scope("acme", "__shared__") == ["acme", "__shared__"]
    assert Neo4jStore._scope("acme", "acme") == ["acme"]       # no self-dup
    assert Neo4jStore._scope("acme", None) == ["acme"]          # disabled / not provided
