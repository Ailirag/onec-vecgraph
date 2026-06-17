"""Overlay indexing driver: (re)index only the touched objects of a developer's XML working
tree into an ephemeral overlay tenant, and tombstone deletions.

Per-object upsert — never a full reset of a baseline tenant. Reuses the normal
parse → graph → callgraph → vectorize pipeline, scoped to the touched objects (which are the
only objects an overlay tenant holds, so callgraph/vectorize over the overlay tenant stay small).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .graph.builder import build_graph
from .overlay import _norm, fqn_from_object_key, is_overlay_tenant
from .parsing import discover_parts, enumerate_objects, parse_objects
from .storage import Neo4jStore


def index_overlay(
    settings: Settings,
    *,
    tenant_id: str,
    roots: list[str],
    files: list[dict] | None = None,
    base_tenant_id: str | None = None,
    deleted: list[str] | None = None,
    options: dict | None = None,
) -> dict[str, Any]:
    """Index `files` (touched objects) into the overlay `tenant_id`; tombstone `deleted`.

    `files`: [{key, path, kind?, name?}] — `path` is an absolute dev-tree path, `key` the
    delta object-key. `deleted`: object-keys removed in the dev tree. `options`: build_graph,
    vectorize (default both True). Returns a structured summary.
    """
    if not is_overlay_tenant(tenant_id):
        raise ValueError(f"tenant_id must be an overlay tenant (contain '@task/'): {tenant_id!r}")
    files = files or []
    deleted = deleted or []
    options = options or {}
    do_graph = bool(options.get("build_graph", True))
    do_vec = bool(options.get("vectorize", True))

    # One enumeration pass over the dev roots → fqn↔ref and path→fqn indexes (no XML parse here).
    parts_all: list = []
    fqn_to_ref: dict[str, tuple] = {}
    xml_to_fqn: dict[str, str] = {}
    dirs: list[tuple[str, str]] = []
    for root in roots:
        parts = discover_parts(Path(root))
        parts_all.extend(parts)
        for ref in enumerate_objects(parts):
            fqn_to_ref[ref[0]] = ref
            xml_to_fqn[_norm(ref[1])] = ref[0]
            dirs.append((_norm(ref[2]) + "/", ref[0]))
    dirs.sort(key=lambda kv: len(kv[0]), reverse=True)  # longest object-dir prefix wins

    def resolve(path: str | None, key: str | None) -> str | None:
        if path:
            np = _norm(path)
            if np in xml_to_fqn:
                return xml_to_fqn[np]
            for prefix, fqn in dirs:
                if np.startswith(prefix):
                    return fqn
        return fqn_from_object_key(key) if key else None

    touched: set[str] = set()
    unresolved: list[str] = []
    for f in files:
        fq = resolve(f.get("path"), f.get("key"))
        (touched.add(fq) if fq else unresolved.append(f.get("key") or f.get("path")))
    touched_fqns = sorted(touched)
    deleted_fqns = sorted({fq for k in deleted if (fq := fqn_from_object_key(k))})
    touched_refs = [fqn_to_ref[fq] for fq in touched_fqns if fq in fqn_to_ref]

    with Neo4jStore.from_settings(settings) as store:
        store.ensure_schema()
        for fq in touched_fqns:
            store.scoped_delete_object(tenant_id, fq)  # upsert touched objects (keeps modules/chunks)
        for fq in deleted_fqns:
            # Remove deleted objects from the overlay ENTIRELY (node + modules + chunks) so the
            # callgraph/vectorize rebuild below can't resurrect them; the tombstone masks baseline.
            store.delete_object_full(tenant_id, fq)
        if touched_refs:
            store.write_graph(build_graph(parse_objects(tenant_id, parts_all, touched_refs)))
        store.clear_tombstones(tenant_id, touched_fqns)  # a resurrected object loses its tombstone
        n_tomb = store.write_tombstones(tenant_id, deleted_fqns)

    graph_updated = False
    if do_graph and touched_refs:
        from .callgrapher import build_call_graph

        build_call_graph(tenant_id, settings, reset=True)  # overlay holds only touched → small
        graph_updated = True
    chunks = 0
    dim = None
    if do_vec and touched_refs:
        from .vectorizer import vectorize as run_vectorize

        res = run_vectorize(tenant_id, settings, reset=True, code=True)
        chunks = res.get("chunks_written", 0)
        dim = res.get("dimensions")

    return {
        "tenant_id": tenant_id,
        "base_tenant_id": base_tenant_id,
        "indexed_objects": len(touched_refs),
        "indexed_files": len(files),
        "deleted": n_tomb,
        "chunks": chunks,
        "graph_updated": graph_updated,
        "embedding_model": settings.embedding_model,
        "embedding_dim": dim,
        "unresolved": unresolved,
    }
