"""Baseline (re)index driver: wrap the existing index → callgraph → vectorize pipeline as a
background job for the admin MCP endpoint.

Reuses :func:`indexer.index_dump`, :func:`callgrapher.build_call_graph` and
:func:`vectorizer.vectorize` DIRECTLY (the library functions — never the CLI commands, which call
``os._exit`` and would kill the long-lived server). The request guards are kept as a pure function
(:func:`validate_reindex_request`) so they unit-test without Neo4j.

Empty-graph / files-missing signalling is the point of this module: the pilot's worst bug is a
mount-path desync that produces a "successful" run with an empty graph. We detect a missing/empty
dump path and a zero-object index, flag them, and :func:`final_status` maps either to ``warning``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings
from .jobs import (
    PHASE_CALLGRAPH,
    PHASE_DONE,
    PHASE_INDEX,
    PHASE_VECTORIZE,
    STATUS_SUCCEEDED,
    STATUS_WARNING,
)
from .overlay import is_overlay_tenant

VALID_STEPS = ("index", "callgraph", "vectorize")


def _resolve_steps(options: dict[str, Any]) -> list[str]:
    """Ordered, validated subset of VALID_STEPS (default: all three, in pipeline order)."""
    raw = options.get("steps")
    if raw is None:
        return list(VALID_STEPS)
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"options.steps must be a non-empty subset of {list(VALID_STEPS)}")
    bad = [s for s in raw if s not in VALID_STEPS]
    if bad:
        raise ValueError(f"unknown step(s) {bad}; valid steps are {list(VALID_STEPS)}")
    # Keep canonical pipeline order regardless of how the caller listed them.
    return [s for s in VALID_STEPS if s in set(raw)]


def validate_reindex_request(
    settings: Settings,
    *,
    tenant_id: str,
    source: str | None = None,
    roots: list[str] | None = None,
    options: dict[str, Any] | None = None,
    authorized_base: str | None = None,
) -> str:
    """Validate a baseline-reindex request (pure; no Neo4j). Returns the resolved dump path.

    Raises ValueError (→ MCP isError) when: the server has baseline reindex disabled; `tenant_id` is
    an overlay tenant (overlays go through `index_overlay`); the admin token does not authorize this
    base; no dump `source`/`roots` is given; `steps` is invalid; or `reset` is requested without the
    explicit `confirm_reset` guard. Note: a path that is missing/empty is NOT rejected here — that is
    a runtime ``files_missing`` → ``warning`` (a mount desync), distinct from a malformed request.
    """
    if not settings.baseline_reindex_enabled:
        raise ValueError(
            "baseline reindex is disabled on this server (set BASELINE_REINDEX_ENABLED=true)")
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if is_overlay_tenant(tenant_id):
        raise ValueError(
            f"tenant_id must be a BASELINE tenant, not an overlay ('@task/...'): {tenant_id!r}. "
            "Index overlays via the write server's index_overlay tool.")
    if authorized_base is not None and tenant_id != authorized_base:
        raise ValueError(
            f"admin token is not authorized to baseline-reindex {tenant_id!r} "
            f"(authorized for {authorized_base!r})")

    options = options or {}
    _resolve_steps(options)  # validates step names
    if bool(options.get("reset")) and not bool(options.get("confirm_reset")):
        raise ValueError(
            "reset=true performs a full tenant wipe — pass options.confirm_reset=true to confirm")

    path = source or (roots[0] if roots else None)
    if not path:
        raise ValueError("provide 'source' (the dump directory path) or 'roots'")
    return path


def final_status(summary: dict[str, Any]) -> str:
    """Map a finished reindex summary to a terminal status: warning on empty/missing, else succeeded."""
    if summary.get("files_missing") or summary.get("empty_graph"):
        return STATUS_WARNING
    return STATUS_SUCCEEDED


def run_baseline_reindex(
    settings: Settings,
    *,
    tenant_id: str,
    path: str,
    base_tenant_id: str | None = None,
    options: dict[str, Any] | None = None,
    on_progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Run index → callgraph → vectorize for a baseline tenant; report phases via `on_progress`.

    `on_progress(phase=..., counts={...}, percent=..., **fields)` is called at each phase boundary so
    the job's live status reflects progress. Returns the structured summary (the contract fields, in
    the spirit of the overlay `index_overlay` response).
    """
    options = options or {}
    report = on_progress or (lambda **_kw: None)
    steps = _resolve_steps(options)
    reset = bool(options.get("reset"))

    # Per-job, non-mutating overrides (batch size, embedding model). The dimension MUST still match
    # the single Neo4j vector index — an embedding_model override is an operator's responsibility.
    overrides: dict[str, Any] = {}
    if options.get("batch_size"):
        overrides["embedding_batch_size"] = int(options["batch_size"])
    if options.get("embedding_model"):
        overrides["embedding_model"] = str(options["embedding_model"])
    s = settings.model_copy(update=overrides) if overrides else settings

    summary: dict[str, Any] = {
        "tenant_id": tenant_id,
        "base_tenant_id": base_tenant_id,
        "steps": steps,
        "reset": reset,
        "indexed_objects": None,
        "nodes": None,
        "edges": None,
        "routines": None,
        "chunks": None,
        "graph_updated": False,
        "embedding_model": s.embedding_model,
        "embedding_dim": None,
        "unresolved": [],
        "parse_errors": 0,
        "files_missing": False,
        "empty_graph": False,
    }

    from .parsing import discover_parts

    p = Path(path)
    parts = discover_parts(p) if p.is_dir() else []
    if not parts:
        # Missing dir or a directory with no Configurator parts → the mount-desync failure mode.
        summary["files_missing"] = True
        summary["empty_graph"] = True
        summary["error"] = f"dump path missing or contains no configuration parts: {path}"
        report(phase=PHASE_DONE, percent=100, files_missing=True, empty_graph=True)
        return summary

    total = len(steps)
    done = 0

    if "index" in steps:
        from .indexer import index_dump

        report(phase=PHASE_INDEX)
        res = index_dump(path, tenant_id, s, reset=reset)
        objects = res["counts"]["real_objects"]
        nodes = res["written"]["nodes"]
        edges = res["written"]["edges"]
        summary.update(indexed_objects=objects, nodes=nodes, edges=edges,
                       parse_errors=res.get("parse_errors", 0))
        if objects == 0 or nodes == 0:
            summary["empty_graph"] = True  # indexed "successfully" but produced nothing → warning
        done += 1
        report(phase=PHASE_INDEX, percent=int(100 * done / total), empty_graph=summary["empty_graph"],
               counts={"objects": objects, "nodes": nodes, "edges": edges})

    if "callgraph" in steps:
        from .callgrapher import build_call_graph

        report(phase=PHASE_CALLGRAPH)
        res = build_call_graph(tenant_id, s, reset=True)
        routines = res.get("routines")
        summary.update(routines=routines, graph_updated=True)
        done += 1
        report(phase=PHASE_CALLGRAPH, percent=int(100 * done / total), counts={"routines": routines})

    if "vectorize" in steps:
        from .vectorizer import vectorize

        report(phase=PHASE_VECTORIZE)
        res = vectorize(tenant_id, s, reset=True, code=True)
        chunks = res.get("chunks_written")
        summary.update(chunks=chunks, embedding_dim=res.get("dimensions"),
                       embedding_model=res.get("model", s.embedding_model))
        done += 1
        report(phase=PHASE_VECTORIZE, percent=int(100 * done / total), counts={"chunks": chunks})

    report(phase=PHASE_DONE, percent=100, embedding_model=summary["embedding_model"],
           embedding_dim=summary["embedding_dim"], files_missing=summary["files_missing"],
           empty_graph=summary["empty_graph"])
    return summary
