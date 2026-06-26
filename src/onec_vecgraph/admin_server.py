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

from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import __version__, tenancy
from .baseline import final_status, run_baseline_reindex, validate_reindex_request
from .config import get_settings
from .dashboard import render_page, render_rows
from .jobs import BaselineJob, BaselineRunner, JobSpec, JobStore
from .overlay import base_tenant_of
from .storage import Neo4jStore

settings = get_settings()

ADMIN_INSTRUCTIONS = """\
onec-vecgraph эндпоинт ADMIN / BASELINE-РЕИНДЕКСА — отдельный от read-only сервера запросов и сервера
overlay-записи. Используется для (пере)сборки полного БАЗОВОГО арендатора из XML-выгрузки Конфигуратора.

`reindex_baseline(tenant_id, source|roots, options?)` запускает index → callgraph → vectorize в фоне и
немедленно возвращает `job_id` (fire-and-poll). Опрашивайте `index_job_status(job_id)` на
фазу/счётчики/сводку, пока статус не станет терминальным (succeeded | warning | failed).
Отсутствующий/пустой путь выгрузки или индекс с нулём объектов возвращается как `warning` с
установленными files_missing/empty_graph — трактуйте как несмонтированный том, НЕ как успех.
Baseline-джобы сериализуются (одновременно выполняется одна; остальные в очереди; вторая джоба для того
же арендатора отклоняется с активным job_id). `reset:true` (полная очистка) требует
options.confirm_reset:true. Требует BASELINE_REINDEX_ENABLED=true; bearer-токен (ADMIN_TOKENS)
авторизует один базовый арендатор. Индексируйте OVERLAY'и через index_overlay сервера записи, не здесь."""

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
@mcp.tool(description="""\
Проверка живости сервера. Возвращает имя и версию сервера.""")
def ping() -> dict[str, Any]:
    """Liveness check. Returns server name and version."""
    return {"status": "ok", "server": "onec-vecgraph-admin", "version": __version__}


@mcp.tool(description="""\
Проверка доступности Neo4j: возвращает редакцию сервера и число узлов.""")
def neo4j_health() -> dict[str, Any]:
    """Check Neo4j connectivity and report server edition and node count."""
    with Neo4jStore.from_settings(settings) as store:
        return store.health()


@mcp.tool(description="""\
Сообщает базу, которую этот токен может реиндексировать (None в dev/без-токена режиме), и флаги сервера.""")
def whoami(ctx: Context) -> dict[str, Any]:
    """Report the base this token may baseline-reindex (None in dev/no-token mode) and server flags."""
    base = tenancy.resolve_admin_base(ctx, settings)
    return {
        "authorized_base": base,
        "baseline_reindex_enabled": settings.baseline_reindex_enabled,
        "active_jobs": runner.store.count_active(),
    }


# ── baseline reindex (fire-and-poll) ────────────────────────────────────
@mcp.tool(description="""\
Запустить полный БАЗОВЫЙ (пере)индекс `tenant_id` и немедленно вернуть хэндл джобы.

Выполняет index → callgraph → vectorize в фоне (часы на масштабе ERP) — опрашивайте возвращённый
`job_id` через `index_job_status`. Укажите каталог выгрузки как `source` (предпочтительно) или `roots`
(используется первый элемент; сама выгрузка обнаруживает базу + расширения). `options`:
{steps:["index","callgraph","vectorize"], reset:false, confirm_reset:false, batch_size?,
embedding_model?}.

Возвращает {accepted:true, job_id, status, queue_position} при приёме, либо {accepted:false,
rejected:true, active_job_id}, если у этого арендатора уже есть активная джоба (опрашивайте её).

Требует BASELINE_REINDEX_ENABLED=true; bearer-токен (ADMIN_TOKENS) должен авторизовать базу `tenant_id`.
`tenant_id` должен быть БАЗОВЫМ арендатором (overlay'и идут через index_overlay). `reset:true` требует
options.confirm_reset:true. Ошибки возвращаются как MCP isError.""")
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


@mcp.tool(description="""\
Статус джобы базового (пере)индекса: {status, phase, counts{objects,nodes,edges,routines,chunks},
percent, queue_position, started_at, finished_at, error, embedding_model, embedding_dim, files_missing,
empty_graph, summary}. Опрашивайте, пока статус не станет терминальным (succeeded|warning|failed). Джоба
в очереди сообщает свою позицию; warning означает files_missing/empty_graph (несмонтированный том).

Admin-токен (если настроен) может читать только джобы под своей авторизованной базой.""")
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
