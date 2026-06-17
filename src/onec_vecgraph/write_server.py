"""Overlay WRITE server — a separate, opt-in FastMCP endpoint.

The query server (`server.py`) is strictly read-only. This dedicated endpoint exposes the single
write tool `index_overlay` for the orchestrator's per-task overlay delta. It shares the same Neo4j
but runs on its own port and requires `OVERLAY_WRITE_ENABLED=true`. Writes are confined to overlay
tenants ('<base>@task/*'); a bearer token authorizes one base namespace (`WRITE_AUTH_TOKENS`).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import tenancy
from .config import get_settings
from .overlay import in_namespace, is_overlay_tenant
from .overlay_index import index_overlay as _run_index_overlay

settings = get_settings()

WRITE_INSTRUCTIONS = """\
onec-vecgraph OVERLAY-WRITE endpoint — separate from the read-only query server (hybrid_search etc).

Single tool `index_overlay`: (re)index a per-task overlay delta — the touched objects of a developer's
1C XML working tree — into an ephemeral overlay tenant '<base>@task/<task_id>', and tombstone deletions.
The query server merges baseline ∪ overlay at query time (overlay wins per object; tombstones mask
deletions). Writes are confined to overlay tenants; the bearer token authorizes one base namespace.
Index/vectorize the BASELINE tenant via the CLI, not here."""

mcp = FastMCP(
    "onec-vecgraph-write",
    instructions=WRITE_INSTRUCTIONS,
    host=settings.mcp_host,
    port=settings.write_mcp_port,
    streamable_http_path=settings.mcp_path,
    stateless_http=True,
)


@mcp.tool()
def index_overlay(
    ctx: Context,
    tenant_id: str,
    roots: list[str],
    files: list[dict] | None = None,
    deleted: list[str] | None = None,
    base_tenant_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    base_source: str | None = None,
    dev_source: str | None = None,
    options: dict | None = None,
) -> dict[str, Any]:
    """Incrementally index a per-task overlay delta into an overlay tenant ('<base>@task/<task_id>').

    Indexes ONLY `files` (touched objects of the dev XML tree) — no baseline reset — updating
    graph/code chunks and embeddings with the same model as baseline, and writes tombstones for
    `deleted` so Phase-2 graph queries can mask removed baseline objects. `files`: [{key, path,
    kind?, name?}]; `deleted`: object-keys; `options`: {build_graph, vectorize} (default both true).
    Returns a structured summary (indexed_objects, deleted, chunks, embedding_model/dim, unresolved).

    Requires OVERLAY_WRITE_ENABLED=true; the bearer token must authorize `tenant_id`'s base namespace.
    Errors (disabled, unauthorized, non-overlay tenant, parse/dim issues) come back as MCP isError."""
    if not settings.overlay_write_enabled:
        raise ValueError("overlay write is disabled on this server (set OVERLAY_WRITE_ENABLED=true)")
    authorized = tenancy.resolve_write_base(ctx, settings)  # raises if write-auth set but token bad
    if not is_overlay_tenant(tenant_id):
        raise ValueError(f"tenant_id must be an overlay tenant (contain '@task/'): {tenant_id!r}")
    if authorized is not None and not in_namespace(tenant_id, authorized):
        raise ValueError(f"token not authorized to write overlay tenant {tenant_id!r}")
    return _run_index_overlay(
        settings, tenant_id=tenant_id, base_tenant_id=base_tenant_id,
        roots=roots or [], files=files or [], deleted=deleted or [], options=options or {},
    )


def run(transport: str = "streamable-http") -> None:
    mcp.run(transport=transport)
