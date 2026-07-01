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

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import __version__, tenancy, token_store
from .baseline import final_status, run_baseline_reindex, validate_reindex_request
from .config import get_settings
from .dashboard import render_page, render_rows
from .jobs import BaselineJob, BaselineRunner, JobSpec, JobStore
from .overlay import base_tenant_of
from .storage import Neo4jStore

settings = get_settings()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _self_admin_lookup():
    """Runtime-provisioned token resolver for SELF-admin (reindex/status/whoami of one's OWN tenant).
    Provisioning tools deliberately do NOT use this — control plane stays env-only (ADMIN_TOKENS)."""
    return lambda tok: token_store.lookup_token(tok, settings)

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
authorizes one base tenant. Index OVERLAYS via the write server's index_overlay, not here.

PROVISIONING (opt-in, PROVISIONING_ENABLED=true; control plane = ADMIN_TOKENS): `provision_tenant(
tenant_id, config_id?, display_name?, rotate?)` create-or-returns a tenant and issues a bearer token
mapped to (tenant_id, config_id) — the secret is returned ONCE (created=true or rotate=true), else
token:null; the token works as Bearer on :8000/:8001/:8002 for that tenant and grants additive
__shared__ read. `list_tenants()` audits what is provisioned; `revoke_tenant_token(tenant_id)`
deactivates a project (effective within the token cache TTL). Provisioning is env-admin only — a
provisioned project token cannot provision."""

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
    """Report the base this token may baseline-reindex (None in dev/no-token mode) and server flags.
    Accepts a runtime-provisioned token (self-admin → its own tenant is the base)."""
    base = tenancy.resolve_admin_base(ctx, settings, token_lookup=_self_admin_lookup())
    return {
        "authorized_base": base,
        "baseline_reindex_enabled": settings.baseline_reindex_enabled,
        "provisioning_enabled": settings.provisioning_enabled,
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
    # self-admin: a provisioned token may reindex ITS OWN baseline (authorized base = its tenant).
    authorized = tenancy.resolve_admin_base(ctx, settings, token_lookup=_self_admin_lookup())
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
    authorized = tenancy.resolve_admin_base(ctx, settings, token_lookup=_self_admin_lookup())
    job = runner.store.get(job_id)
    if job is None:
        raise ValueError(f"unknown job_id: {job_id!r}")
    if authorized is not None and base_tenant_of(job.tenant_id) != authorized:
        raise ValueError(f"job {job_id!r} is not under the authorized base")
    return job.snapshot()


# ── runtime tenant provisioning (opt-in: PROVISIONING_ENABLED; control plane = env ADMIN_TOKENS) ──
def _should_issue(exists: bool, rotate: bool) -> bool:
    """Strict issue policy: mint a new token only on first creation or an explicit rotate.
    The secret is shown exactly once, so an existing tenant returns token:null unless rotate=true
    (a revoked tenant recovers via rotate=true)."""
    return (not exists) or rotate


def _require_provisioner(ctx: Context) -> None:
    """Authorize a provisioning call: env ADMIN_TOKENS control plane (or trusted mode if unset).
    Deliberately does NOT consult the provisioned-token store, so a data-plane project token can
    never provision/list/revoke other tenants."""
    tenancy.resolve_admin_base(ctx, settings)  # raises if admin-auth in force but token bad/missing


@mcp.tool()
def provision_tenant(
    ctx: Context,
    tenant_id: str,
    config_id: str | None = None,
    display_name: str | None = None,
    rotate: bool = False,
) -> dict[str, Any]:
    """Create-or-return a tenant and issue a bearer token mapped to (tenant_id, config_id).

    Idempotent. `created=false` if the tenant already existed. `rotate=true` mints a NEW token and
    invalidates the old one. The secret `token` is returned EXACTLY ONCE (only its sha256 is stored):
    on first creation or rotate it is the new token; otherwise it is null. The issued token is usable
    as `Authorization: Bearer` on the read (:8000), overlay-write (:8001) and admin (:8002, self)
    endpoints for THIS tenant, and grants additive `__shared__` read (platform/standards discovery).

    Requires PROVISIONING_ENABLED=true; authorized by the admin control plane (ADMIN_TOKENS, or
    trusted mode when no admin tokens are configured). Errors come back as MCP isError."""
    if not settings.provisioning_enabled:
        raise ValueError("provisioning is disabled on this server (set PROVISIONING_ENABLED=true)")
    _require_provisioner(ctx)
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required")
    if tid == settings.shared_tenant_id:
        raise ValueError(f"cannot provision the shared public tenant {tid!r}")
    cfg = (config_id or "").strip() or settings.default_config_id
    now = _utcnow_iso()
    with Neo4jStore.from_settings(settings) as store:
        store.ensure_schema()  # idempotent; makes the admin endpoint self-sufficient
        exists = store.read(
            "MATCH (t:Tenant {tenant_id: $tid}) RETURN count(t) > 0 AS exists", tid=tid
        )[0]["exists"]
        issue = _should_issue(exists, rotate)
        token = secrets.token_urlsafe(32) if issue else None
        token_hash = hashlib.sha256(token.encode()).hexdigest() if token else None
        result = store.provision_tenant(tid, cfg, display_name, token_hash, now)
    if issue:
        token_store.invalidate(tid)  # local cache; other processes converge within TTL
    return {
        "tenant_id": tid,
        "config_id": result["config_id"],
        "token": token,  # returned once; null when created=false and rotate=false
        "created": result["created"],
    }


@mcp.tool()
def list_tenants(ctx: Context) -> dict[str, Any]:
    """Audit: every provisioned tenant with its config_ids and whether an active bearer token exists.
    No secrets are returned. Requires PROVISIONING_ENABLED=true; admin control-plane authorized."""
    if not settings.provisioning_enabled:
        raise ValueError("provisioning is disabled on this server (set PROVISIONING_ENABLED=true)")
    _require_provisioner(ctx)
    with Neo4jStore.from_settings(settings) as store:
        rows = store.list_tenants()
    return {
        "tenants": [
            {
                "tenant_id": r["tenant_id"],
                "config_ids": sorted({c for c in r["config_ids_raw"] if c}),
                "has_token": r["has_token"],
            }
            for r in rows
        ]
    }


@mcp.tool()
def revoke_tenant_token(ctx: Context, tenant_id: str) -> dict[str, Any]:
    """Deactivate the tenant's active bearer token(s) (deactivate a project). Takes effect on the
    read/write servers within the token cache TTL (~token_cache_ttl_seconds). Returns the count
    revoked. Requires PROVISIONING_ENABLED=true; admin control-plane authorized."""
    if not settings.provisioning_enabled:
        raise ValueError("provisioning is disabled on this server (set PROVISIONING_ENABLED=true)")
    _require_provisioner(ctx)
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required")
    with Neo4jStore.from_settings(settings) as store:
        n = store.revoke_tenant_token(tid, _utcnow_iso())
    token_store.invalidate(tid)
    return {"tenant_id": tid, "revoked": n}


# ── read-only web dashboard (opt-in: ADMIN_DASHBOARD_ENABLED) ───────────
def _job_snapshots() -> list[dict[str, Any]]:
    return [j.snapshot() for j in runner.store.list_all()]


@mcp.custom_route("/jobs", methods=["GET"])
async def jobs_dashboard(request: Request) -> Response:
    """Read-only HTML dashboard of baseline jobs. `?partial=1` returns just the table body (for the
    page's live in-place refresh). Disabled (404) unless ADMIN_DASHBOARD_ENABLED=true. Unauthenticated
    — keep it on loopback / behind an authenticating proxy."""
    if not settings.admin_dashboard_enabled:
        return PlainTextResponse("dashboard is disabled (set ADMIN_DASHBOARD_ENABLED=true)", status_code=404)
    snapshots = _job_snapshots()
    if request.query_params.get("partial"):
        return HTMLResponse(render_rows(snapshots))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return HTMLResponse(render_page(snapshots, generated_at=now, active=runner.store.count_active()))


@mcp.custom_route("/jobs.json", methods=["GET"])
async def jobs_json(request: Request) -> Response:
    """Machine-readable job list (same data as the dashboard). 404 unless ADMIN_DASHBOARD_ENABLED=true."""
    if not settings.admin_dashboard_enabled:
        return JSONResponse({"error": "dashboard disabled"}, status_code=404)
    return JSONResponse({
        "jobs": _job_snapshots(),
        "active": runner.store.count_active(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def run(transport: str = "streamable-http") -> None:
    mcp.run(transport=transport)
