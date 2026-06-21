"""Admin / baseline-reindex MCP server — a separate, opt-in maintenance endpoint.

The query server (`server.py`) is strictly read-only; the overlay-write server (`write_server.py`)
only touches per-task overlays. This third endpoint exists for the orchestrator to run and MONITOR a
full BASELINE (re)index — index → callgraph → vectorize of a baseline tenant — without `docker exec`
and without holding a connection for hours. It runs on its own port and requires
`BASELINE_REINDEX_ENABLED=true`.

Two tools drive a fire-and-poll lifecycle:
  • `reindex_baseline(...)` → returns a `job_id` immediately; the work runs in a background worker.
  • `index_job_status(job_id)` → poll phase/counts/summary until a terminal status.
plus `ping` / `neo4j_health` / `whoami` for the orchestrator's readiness probe. Baseline jobs are
serialized server-side (one shared GPU); an admin bearer token authorizes one base (`ADMIN_TOKENS`).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import __version__, tenancy
from .baseline import final_status, run_baseline_reindex, validate_reindex_request
from .config import get_settings
from .jobs import BaselineJob, BaselineRunner, JobSpec, JobStore
from .overlay import base_tenant_of
from .storage import Neo4jStore

settings = get_settings()

ADMIN_INSTRUCTIONS = """\
onec-vecgraph ADMIN / BASELINE-REINDEX endpoint — separate from the read-only query server and the
overlay-write server. Use it to (re)build a full BASELINE tenant from a Configurator XML dump.

`reindex_baseline(tenant_id, source|roots, options?)` runs index → callgraph → vectorize in the
background and returns a `job_id` immediately (fire-and-poll). Poll `index_job_status(job_id)` for
phase/counts/summary until status is terminal (succeeded | warning | failed). A missing/empty dump
path or a zero-object index comes back as `warning` with files_missing/empty_graph set — treat it as
a failed mount, NOT a success. Baseline jobs are serialized (one runs at a time; others queue; a
second job for the same tenant is rejected with the active job_id). `reset:true` (full wipe) requires
options.confirm_reset:true. Requires BASELINE_REINDEX_ENABLED=true; the bearer token (ADMIN_TOKENS)
authorizes one base tenant. Index OVERLAYS via the write server's index_overlay, not here."""

mcp = FastMCP(
    "onec-vecgraph-admin",
    instructions=ADMIN_INSTRUCTIONS,
    host=settings.mcp_host,
    port=settings.admin_mcp_port,
    streamable_http_path=settings.mcp_path,
    stateless_http=True,
)


def _execute(job: BaselineJob, on_progress: Any) -> dict[str, Any]:
    """Bridge a queued job to the reindex driver (runs in the runner's worker thread)."""
    return run_baseline_reindex(
        settings, tenant_id=job.tenant_id, path=job.path or "",
        base_tenant_id=job.base_tenant_id, options=job.options, on_progress=on_progress,
    )


# Module-level singleton: survives across stateless MCP calls within one running server, so polling
# `index_job_status` after `reindex_baseline` sees the same job. Persists to JSON if configured.
runner = BaselineRunner(JobStore(settings.baseline_jobs_path), execute=_execute, classify=final_status)


# ── health / introspection (readiness probe) ───────────────────────────
@mcp.tool()
def ping() -> dict[str, Any]:
    """Liveness check. Returns server name and version."""
    return {"status": "ok", "server": "onec-vecgraph-admin", "version": __version__}


@mcp.tool()
def neo4j_health() -> dict[str, Any]:
    """Check Neo4j connectivity and report server edition and node count."""
    with Neo4jStore.from_settings(settings) as store:
        return store.health()


@mcp.tool()
def whoami(ctx: Context) -> dict[str, Any]:
    """Report the base this token may baseline-reindex (None in dev/no-token mode) and server flags."""
    base = tenancy.resolve_admin_base(ctx, settings)
    return {
        "authorized_base": base,
        "baseline_reindex_enabled": settings.baseline_reindex_enabled,
        "active_jobs": runner.store.count_active(),
    }


# ── baseline reindex (fire-and-poll) ────────────────────────────────────
@mcp.tool()
def reindex_baseline(
    ctx: Context,
    tenant_id: str,
    source: str | None = None,
    roots: list[str] | None = None,
    base_tenant_id: str | None = None,
    options: dict | None = None,
) -> dict[str, Any]:
    """Start a full BASELINE (re)index of `tenant_id` and return a job handle immediately.

    Runs index → callgraph → vectorize in the background (hours on ERP scale) — poll the returned
    `job_id` via `index_job_status`. Provide the dump directory as `source` (preferred) or `roots`
    (first entry used; the dump itself discovers base + extensions). `options`: {steps:["index",
    "callgraph","vectorize"], reset:false, confirm_reset:false, batch_size?, embedding_model?}.

    Returns {accepted:true, job_id, status, queue_position} on acceptance, or {accepted:false,
    rejected:true, active_job_id} if this tenant already has an active job (poll that one instead).

    Requires BASELINE_REINDEX_ENABLED=true; the bearer token (ADMIN_TOKENS) must authorize
    `tenant_id`'s base. `tenant_id` must be a BASELINE tenant (overlays go through index_overlay).
    `reset:true` requires options.confirm_reset:true. Errors come back as MCP isError."""
    if not settings.baseline_reindex_enabled:
        raise ValueError("baseline reindex is disabled on this server (set BASELINE_REINDEX_ENABLED=true)")
    authorized = tenancy.resolve_admin_base(ctx, settings)  # raises if admin-auth set but token bad
    path = validate_reindex_request(
        settings, tenant_id=tenant_id, source=source, roots=roots,
        options=options, authorized_base=authorized,
    )
    spec = JobSpec(tenant_id=tenant_id, path=path, base_tenant_id=base_tenant_id, options=options or {})
    return runner.submit(spec)


@mcp.tool()
def index_job_status(ctx: Context, job_id: str) -> dict[str, Any]:
    """Status of a baseline (re)index job: {status, phase, counts{objects,nodes,edges,routines,
    chunks}, percent, queue_position, started_at, finished_at, error, embedding_model, embedding_dim,
    files_missing, empty_graph, summary}. Poll until status is terminal (succeeded|warning|failed).
    A queued job reports its position; warning means files_missing/empty_graph (a failed mount).

    The admin token (if configured) may only read jobs under its authorized base."""
    authorized = tenancy.resolve_admin_base(ctx, settings)
    job = runner.store.get(job_id)
    if job is None:
        raise ValueError(f"unknown job_id: {job_id!r}")
    if authorized is not None and base_tenant_of(job.tenant_id) != authorized:
        raise ValueError(f"job {job_id!r} is not under the authorized base")
    return job.snapshot()


def run(transport: str = "streamable-http") -> None:
    mcp.run(transport=transport)
