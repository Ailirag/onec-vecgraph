"""Index a 1C Configurator XML dump into Neo4j: parse -> build graph -> write.

Supports full indexing and incremental re-indexing (only objects whose configVersion
in ConfigDumpInfo.xml changed; removes objects deleted from the dump).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import Settings
from .graph.builder import build_graph
from .parsing import discover_parts, enumerate_objects, parse_config, parse_objects
from .storage import Neo4jStore

log = logging.getLogger(__name__)


def _parts_summary(parts: list) -> list[dict[str, Any]]:
    return [
        {"config_id": p.config_id, "name": p.name, "extension": p.is_extension, "purpose": p.purpose}
        for p in parts
    ]


def index_dump(
    path: str | Path,
    tenant_id: str,
    settings: Settings,
    reset: bool = False,
    incremental: bool = False,
    config_release: str | None = None,
) -> dict[str, Any]:
    root = Path(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Dump path is not a directory: {root}")

    parts = discover_parts(root)

    if incremental and not reset:
        return _index_incremental(root, tenant_id, settings, parts, config_release=config_release)

    parsed = parse_config(root, tenant_id, progress_label=f"index:{tenant_id}")
    graph = build_graph(parsed, config_release=config_release)
    with Neo4jStore.from_settings(settings) as store:
        store.ensure_schema()
        if reset:
            store.delete_tenant(tenant_id)
        n_nodes = sum(len(rows) for rows in graph.nodes.values())
        n_edges = sum(len(g.rows) for g in graph.edges.values())
        log.info("[index:%s] запись графа в Neo4j: %s узлов, %s рёбер…",
                 tenant_id, f"{n_nodes:,}", f"{n_edges:,}")
        written = store.write_graph(graph)
        counts = store.counts(tenant_id)

    return {
        "mode": "full",
        "tenant_id": tenant_id,
        "parts": _parts_summary(parts),
        "object_files_seen": parsed.files_seen,
        "objects_parsed": len(parsed.objects),
        "parse_errors": len(parsed.errors),
        "parse_error_sample": parsed.errors[:10],
        "written": written,
        "counts": counts,
    }


def _index_incremental(root: Path, tenant_id: str, settings: Settings, parts: list,
                       config_release: str | None = None) -> dict[str, Any]:
    with Neo4jStore.from_settings(settings) as store:
        store.ensure_schema()
        stored = store.object_versions(tenant_id)
        refs = enumerate_objects(parts)  # (fqn, xml, object_dir, config_id, config_version)

        # Group refs by fqn: a borrowed (Adopted) object shares its fqn across parts (base +
        # extension). If ANY part changed, the FULL object is rebuilt from ALL its parts — reparsing
        # only the changed extension part would scoped-delete the merged object and lose the
        # base-contributed sub-nodes (fields/forms/…). For single-part configs this is identical
        # to the old per-ref behaviour.
        refs_by_fqn: dict[str, list] = {}
        for ref in refs:
            refs_by_fqn.setdefault(ref[0], []).append(ref)
        current_fqns = set(refs_by_fqn)

        # An fqn is changed if any of its parts is new or its configVersion differs. Hashless parts
        # (no configVersion, e.g. nested subsystems) are reindexed only when NEW (else they'd churn).
        def _changed(fqn: str, group: list) -> bool:
            return any(
                (r[4] is None and fqn not in stored) or (r[4] is not None and stored.get(fqn) != r[4])
                for r in group
            )
        changed_fqns = [fqn for fqn, group in refs_by_fqn.items() if _changed(fqn, group)]
        deleted = [fqn for fqn in stored if fqn not in current_fqns]
        changed_refs = [r for fqn in changed_fqns for r in refs_by_fqn[fqn]]

        parsed = parse_objects(tenant_id, parts, changed_refs,
                               progress_label=f"index:{tenant_id} (incr)" if changed_refs else None)
        graph = build_graph(parsed, config_release=config_release)

        for fqn in changed_fqns:
            store.scoped_delete_object(tenant_id, fqn)
        for fqn in deleted:
            store.delete_object_full(tenant_id, fqn)

        written = store.write_graph(graph) if changed_refs else {"nodes": 0, "edges": 0}
        counts = store.counts(tenant_id)

    return {
        "mode": "incremental",
        "tenant_id": tenant_id,
        "parts": _parts_summary(parts),
        "objects_total": len(current_fqns),
        "changed": len(changed_fqns),
        "deleted": len(deleted),
        "unchanged": len(current_fqns) - len(changed_fqns),
        "parse_errors": len(parsed.errors),
        "parse_error_sample": parsed.errors[:10],
        "written": written,
        "counts": counts,
    }
