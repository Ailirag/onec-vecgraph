"""Bearer-token → tenant lookup backed by Neo4j, with a small in-process TTL cache.

The read (:8000) and write (:8001) servers call `lookup_token(token, settings)` ONLY on an
env-token-map miss (env tokens always win). Provisioned tokens live in Neo4j as `:TenantToken`
nodes (sha256 hash only) created by the admin server's `provision_tenant`.

Caching: results (including negatives) are cached per token_hash for `token_cache_ttl_seconds`
(~30s). This bounds Neo4j round-trips to ~1 per token per TTL window. A revoked or rotated token
therefore keeps working on an already-running process for at most one TTL window — this staleness
is the documented contract; keep the TTL small. `invalidate()` clears the local cache (best-effort,
this process only; other processes converge by TTL).

The Neo4j driver here is a process-wide lazy singleton (its own connection pool), independent of the
per-tool `Neo4jStore.from_settings(...)` handles, so the auth path never creates a driver per request.
"""

from __future__ import annotations

import atexit
import hashlib
import threading
import time
from typing import Any

from .config import Settings
from .storage import Neo4jStore

_store: Neo4jStore | None = None
_store_lock = threading.Lock()

# token_hash -> (expiry_monotonic, {tenant_id, config_id} | None)
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_cache_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_store(settings: Settings) -> Neo4jStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Neo4jStore.from_settings(settings)
                atexit.register(_close)
    return _store


def _close() -> None:
    global _store
    if _store is not None:
        try:
            _store.close()
        except Exception:  # noqa: BLE001 - best-effort on shutdown
            pass
        _store = None


def lookup_token(token: str, settings: Settings) -> dict[str, Any] | None:
    """Return {tenant_id, config_id} for an ACTIVE provisioned token, else None. TTL-cached."""
    if not token:
        return None
    h = _hash(token)
    now = time.monotonic()
    hit = _cache.get(h)
    if hit is not None and hit[0] > now:
        return hit[1]
    value = _get_store(settings).resolve_token(h)
    with _cache_lock:
        _cache[h] = (now + max(1, settings.token_cache_ttl_seconds), value)
    return value


def invalidate(tenant_id: str | None = None) -> None:
    """Best-effort local cache clear after revoke/rotate. token_hash isn't reversible to a tenant,
    so we clear the whole cache; other server processes converge within the TTL window."""
    with _cache_lock:
        _cache.clear()
