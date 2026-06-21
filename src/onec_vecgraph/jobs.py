"""Baseline-reindex job store + single-flight runner (no Neo4j dependency).

The admin endpoint (`admin_server.py`) exposes a fire-and-poll baseline reindex: a call returns a
`job_id` immediately and the heavy work (index → callgraph → vectorize, hours on ERP scale) runs in
a background worker. The orchestrator polls `index_job_status(job_id)`.

A single worker thread drains a FIFO queue, so baseline jobs are **serialized server-side** — one
onec-vecgraph serves a pool of tenants on a shared GPU, so two baseline reindexes must never run at
once. A second job for a different tenant is queued; a second job for a tenant that already has an
active job is rejected (pointing at the active job_id).

State lives in an in-process :class:`JobStore` (survives across MCP calls within one running server).
If a persist path is configured it is mirrored to JSON, so status survives a restart — jobs that were
mid-flight when the process died are marked ``failed`` on reload (their worker thread is gone).
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── status / phase vocab (the orchestrator switches on these) ───────────
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"
TERMINAL = {STATUS_SUCCEEDED, STATUS_WARNING, STATUS_FAILED}
ACTIVE = {STATUS_QUEUED, STATUS_RUNNING}

PHASE_QUEUED = "queued"
PHASE_INDEX = "index"
PHASE_CALLGRAPH = "callgraph"
PHASE_VECTORIZE = "vectorize"
PHASE_DONE = "done"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_counts() -> dict[str, int | None]:
    # The status contract counts; None until the producing phase fills them.
    return {"objects": None, "nodes": None, "edges": None, "routines": None, "chunks": None}


@dataclass
class BaselineJob:
    """One baseline (re)index job and its live status (serialized verbatim to JSON when persisting)."""

    job_id: str
    tenant_id: str
    base_tenant_id: str | None = None
    path: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_QUEUED
    phase: str = PHASE_QUEUED
    counts: dict[str, int | None] = field(default_factory=_empty_counts)
    percent: int = 0
    queue_position: int = 0
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    files_missing: bool = False
    empty_graph: bool = False
    summary: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        """The `index_job_status` response (a copy — safe to hand out without holding the lock)."""
        return {
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "base_tenant_id": self.base_tenant_id,
            "status": self.status,
            "phase": self.phase,
            "counts": dict(self.counts),
            "percent": self.percent,
            "queue_position": self.queue_position,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "files_missing": self.files_missing,
            "empty_graph": self.empty_graph,
            "summary": self.summary,
        }


class JobStore:
    """Thread-safe store of :class:`BaselineJob`, with optional JSON persistence.

    All mutation goes through :meth:`update` so persistence and locking stay in one place. On
    construction any persisted job left in an active state (queued/running) is marked ``failed`` —
    its worker did not survive the restart.
    """

    _FIELD_NAMES = {f.name for f in fields(BaselineJob)}

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, BaselineJob] = {}
        self._path = Path(persist_path) if persist_path else None
        if self._path and self._path.exists():
            self._load()

    # ── persistence ───────────────────────────────────────────────────
    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # corrupt/unreadable state file → start clean rather than crash the server
        for rec in raw.get("jobs", []):
            data = {k: v for k, v in rec.items() if k in self._FIELD_NAMES}
            job = BaselineJob(**data)
            if job.status in ACTIVE:
                job.status = STATUS_FAILED
                job.error = "server restarted before the job finished"
                job.finished_at = job.finished_at or _now()
            self._jobs[job.job_id] = job

    def _persist_locked(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": [asdict(j) for j in self._jobs.values()]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # atomic swap — a poll never reads a half-written file

    # ── reads ─────────────────────────────────────────────────────────
    def get(self, job_id: str) -> BaselineJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for_tenant(self, tenant_id: str) -> BaselineJob | None:
        """The queued/running job for `tenant_id`, if any (single-flight per tenant)."""
        with self._lock:
            for job in self._jobs.values():
                if job.tenant_id == tenant_id and job.status in ACTIVE:
                    return job
            return None

    def count_active(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in ACTIVE)

    # ── writes ────────────────────────────────────────────────────────
    def add(self, job: BaselineJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist_locked()

    def update(self, job_id: str, *, counts: dict[str, Any] | None = None, **fields_: Any) -> None:
        """Patch a job: merge `counts` partially, set any other BaselineJob field, then persist."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if counts:
                job.counts.update({k: v for k, v in counts.items() if k in job.counts})
            for key, value in fields_.items():
                if key in self._FIELD_NAMES and key != "counts":
                    setattr(job, key, value)
            self._persist_locked()


@dataclass
class JobSpec:
    """A baseline-reindex request validated and accepted by the runner."""

    tenant_id: str
    path: str
    base_tenant_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


class BaselineRunner:
    """Single-flight FIFO runner for baseline jobs (one daemon worker → global serialization).

    `execute(job, on_progress) -> summary` does the actual work and is injected (tests pass a fast
    fake). `classify(summary) -> status` maps a finished summary to a terminal status (default:
    always ``succeeded``); the admin server passes :func:`baseline.final_status`.
    """

    def __init__(
        self,
        store: JobStore,
        execute: Callable[[BaselineJob, Callable[..., None]], dict[str, Any]],
        classify: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self._store = store
        self._execute = execute
        self._classify = classify or (lambda _summary: STATUS_SUCCEEDED)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()        # guards worker start
        self._submit_lock = threading.Lock()  # serializes the single-flight check-and-enqueue

    @property
    def store(self) -> JobStore:
        return self._store

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, name="baseline-runner", daemon=True)
                self._worker.start()

    def submit(self, spec: JobSpec) -> dict[str, Any]:
        """Accept a baseline reindex: enqueue and return immediately (fire-and-poll).

        Single-flight per tenant: if `spec.tenant_id` already has an active job, reject (the caller
        should poll the active one). Otherwise enqueue behind any other tenants' jobs.
        """
        # Hold the submit lock across the whole check-and-enqueue so two concurrent submits for the
        # same tenant cannot both pass the single-flight check (FastMCP may run sync tools in threads).
        with self._submit_lock:
            active = self._store.active_for_tenant(spec.tenant_id)
            if active is not None:
                return {
                    "accepted": False,
                    "rejected": True,
                    "reason": "a baseline job for this tenant is already active",
                    "active_job_id": active.job_id,
                    "status": active.status,
                }
            job = BaselineJob(
                job_id=f"bl-{uuid.uuid4().hex[:12]}",
                tenant_id=spec.tenant_id,
                base_tenant_id=spec.base_tenant_id,
                path=spec.path,
                options=dict(spec.options or {}),
                queue_position=self._store.count_active(),  # jobs ahead of this one
            )
            self._store.add(job)
            self._queue.put(job.job_id)
        self._ensure_worker()
        return {
            "accepted": True,
            "job_id": job.job_id,
            "status": job.status,
            "queue_position": job.queue_position,
        }

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                if job_id is None:  # shutdown sentinel (tests)
                    return
                self._run_one(job_id)
            finally:
                self._queue.task_done()

    def _run_one(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            return
        # status→running now; the concrete phase is set by `execute` as its first action (so a job
        # whose steps skip 'index' isn't briefly mislabelled).
        self._store.update(job_id, status=STATUS_RUNNING, queue_position=0, started_at=_now())

        def on_progress(*, counts: dict[str, Any] | None = None, **fields_: Any) -> None:
            self._store.update(job_id, counts=counts, **fields_)

        try:
            summary = self._execute(job, on_progress)
        except Exception as exc:  # noqa: BLE001 - any failure becomes a terminal 'failed' status
            self._store.update(job_id, status=STATUS_FAILED, phase=PHASE_DONE,
                               error=f"{type(exc).__name__}: {exc}", finished_at=_now())
            return
        summary = summary or {}
        self._store.update(
            job_id,
            status=self._classify(summary),
            phase=PHASE_DONE,
            percent=100,
            finished_at=_now(),
            summary=summary,
            files_missing=bool(summary.get("files_missing")),
            empty_graph=bool(summary.get("empty_graph")),
            embedding_model=summary.get("embedding_model"),
            embedding_dim=summary.get("embedding_dim"),
        )

    # ── test/cleanup helpers ──────────────────────────────────────────
    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until no job is queued/running, or `timeout` elapses (tests; not used by server)."""
        deadline = time.monotonic() + timeout
        while self._store.count_active() > 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def shutdown(self) -> None:
        self._queue.put(None)
