"""Unit tests for the baseline-reindex-over-MCP feature (no Neo4j; pure functions + job runner)."""

from __future__ import annotations

import threading
import time

import pytest

from onec_vecgraph.baseline import (
    _resolve_steps,
    final_status,
    validate_reindex_request,
)
from onec_vecgraph.config import Settings
from onec_vecgraph.jobs import (
    ACTIVE,
    PHASE_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_WARNING,
    BaselineJob,
    BaselineRunner,
    JobSpec,
    JobStore,
)
from onec_vecgraph.tenancy import TenantResolutionError, resolve_admin_base


# ── auth: admin token map + resolution ──────────────────────────────────
class _Ctx:
    """Minimal stand-in for an MCP Context carrying HTTP request headers."""

    def __init__(self, headers: dict[str, str]):
        req = type("Req", (), {"headers": headers})()
        self.request_context = type("RC", (), {"request": req})()


def test_admin_auth_token_map_parses_base() -> None:
    s = Settings(admin_auth_tokens="atok=grand-dev-mdm@release, other=acme@release , bad, =x, y=")
    assert s.admin_auth_token_map() == {"atok": "grand-dev-mdm@release", "other": "acme@release"}


def test_resolve_admin_base_dev_mode_no_tokens() -> None:
    # No admin tokens configured → dev/trusted mode → None (caller still confines to a baseline).
    assert resolve_admin_base(_Ctx({}), Settings()) is None


def test_resolve_admin_base_valid_token() -> None:
    s = Settings(admin_auth_tokens="atok=grand-dev-mdm@release")
    ctx = _Ctx({"Authorization": "Bearer atok"})
    assert resolve_admin_base(ctx, s) == "grand-dev-mdm@release"


def test_resolve_admin_base_rejects_missing_or_bad_token() -> None:
    s = Settings(admin_auth_tokens="atok=grand-dev-mdm@release")
    with pytest.raises(TenantResolutionError):
        resolve_admin_base(_Ctx({}), s)
    with pytest.raises(TenantResolutionError):
        resolve_admin_base(_Ctx({"Authorization": "Bearer wrong"}), s)


# ── request validation guards ───────────────────────────────────────────
def _enabled() -> Settings:
    return Settings(baseline_reindex_enabled=True)


def test_validate_rejects_when_disabled() -> None:
    with pytest.raises(ValueError, match="disabled"):
        validate_reindex_request(Settings(), tenant_id="grand@release", source="/dumps/erp")


def test_validate_rejects_overlay_tenant() -> None:
    with pytest.raises(ValueError, match="overlay"):
        validate_reindex_request(_enabled(), tenant_id="grand@release@task/T-1", source="/dumps/erp")


def test_validate_rejects_unauthorized_base() -> None:
    with pytest.raises(ValueError, match="not authorized"):
        validate_reindex_request(
            _enabled(), tenant_id="acme@release", source="/dumps/erp",
            authorized_base="grand-dev-mdm@release")


def test_validate_requires_source_or_roots() -> None:
    with pytest.raises(ValueError, match="source"):
        validate_reindex_request(_enabled(), tenant_id="grand@release")


def test_validate_rejects_invalid_steps() -> None:
    with pytest.raises(ValueError, match="unknown step"):
        validate_reindex_request(
            _enabled(), tenant_id="grand@release", source="/dumps/erp",
            options={"steps": ["index", "frobnicate"]})


def test_validate_reset_requires_confirm() -> None:
    with pytest.raises(ValueError, match="confirm_reset"):
        validate_reindex_request(
            _enabled(), tenant_id="grand@release", source="/dumps/erp", options={"reset": True})


def test_validate_happy_path_returns_path() -> None:
    assert validate_reindex_request(
        _enabled(), tenant_id="grand@release", source="/dumps/erp") == "/dumps/erp"
    # roots[0] used when source omitted
    assert validate_reindex_request(
        _enabled(), tenant_id="grand@release", roots=["/dumps/erp", "/dumps/other"]) == "/dumps/erp"
    # reset with explicit confirm is allowed (authorized base matches tenant)
    assert validate_reindex_request(
        _enabled(), tenant_id="grand@release", source="/dumps/erp",
        options={"reset": True, "confirm_reset": True}, authorized_base="grand@release") == "/dumps/erp"


# ── steps + final status ────────────────────────────────────────────────
def test_resolve_steps_default_and_order() -> None:
    assert _resolve_steps({}) == ["index", "callgraph", "vectorize"]
    # canonical order regardless of caller order
    assert _resolve_steps({"steps": ["vectorize", "index"]}) == ["index", "vectorize"]


def test_resolve_steps_empty_list_rejected() -> None:
    with pytest.raises(ValueError):
        _resolve_steps({"steps": []})


def test_final_status_warning_on_missing_or_empty() -> None:
    assert final_status({"files_missing": True}) == STATUS_WARNING
    assert final_status({"empty_graph": True}) == STATUS_WARNING
    assert final_status({"indexed_objects": 1200, "nodes": 5000}) == STATUS_SUCCEEDED


# ── JobStore: create / update / persistence ─────────────────────────────
def test_job_store_create_update_counts_merge() -> None:
    store = JobStore()
    job = BaselineJob(job_id="j1", tenant_id="grand@release")
    store.add(job)
    store.update("j1", phase="index", counts={"objects": 10, "nodes": 50})
    store.update("j1", counts={"chunks": 7})  # partial merge keeps prior keys
    snap = store.get("j1").snapshot()
    assert snap["phase"] == "index"
    assert snap["counts"]["objects"] == 10 and snap["counts"]["chunks"] == 7
    assert snap["counts"]["edges"] is None  # untouched key stays None


def test_job_store_json_round_trip(tmp_path) -> None:
    path = tmp_path / "jobs.json"
    s1 = JobStore(path)
    s1.add(BaselineJob(job_id="j1", tenant_id="grand@release", status=STATUS_SUCCEEDED))
    # New store over the same file reloads the job.
    s2 = JobStore(path)
    assert s2.get("j1").status == STATUS_SUCCEEDED


def test_job_store_marks_active_jobs_failed_on_reload(tmp_path) -> None:
    path = tmp_path / "jobs.json"
    s1 = JobStore(path)
    s1.add(BaselineJob(job_id="j1", tenant_id="grand@release", status=STATUS_RUNNING))
    s2 = JobStore(path)  # process "restart"
    job = s2.get("j1")
    assert job.status == STATUS_FAILED
    assert "restart" in (job.error or "")


# ── BaselineRunner: single-flight, transitions, classify ────────────────
def _gated_runner():
    """A runner whose fake execute blocks on a shared gate and tracks max concurrency."""
    gate = threading.Event()
    state = {"concurrent": 0, "max_concurrent": 0, "lock": threading.Lock()}

    def execute(job, on_progress):
        with state["lock"]:
            state["concurrent"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
        on_progress(phase="index", counts={"objects": 1})
        gate.wait(timeout=2.0)
        with state["lock"]:
            state["concurrent"] -= 1
        return {"indexed_objects": 1, "nodes": 1, "embedding_model": "m", "embedding_dim": 8}

    runner = BaselineRunner(JobStore(), execute=execute, classify=final_status)
    return runner, gate, state


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def test_runner_serializes_different_tenants() -> None:
    runner, gate, state = _gated_runner()
    a = runner.submit(JobSpec(tenant_id="t-a", path="/d"))
    b = runner.submit(JobSpec(tenant_id="t-b", path="/d"))
    assert a["accepted"] and b["accepted"]
    # First job starts running; the second waits in the queue behind it.
    assert _wait_until(lambda: runner.store.get(a["job_id"]).status == STATUS_RUNNING)
    assert b["queue_position"] == 1
    assert runner.store.get(b["job_id"]).status in ACTIVE
    gate.set()
    assert runner.wait_idle(timeout=3.0)
    assert runner.store.get(a["job_id"]).status == STATUS_SUCCEEDED
    assert runner.store.get(b["job_id"]).status == STATUS_SUCCEEDED
    assert state["max_concurrent"] == 1  # never two baseline jobs at once
    runner.shutdown()


def test_runner_rejects_duplicate_same_tenant() -> None:
    runner, gate, _ = _gated_runner()
    first = runner.submit(JobSpec(tenant_id="t-a", path="/d"))
    dup = runner.submit(JobSpec(tenant_id="t-a", path="/d"))
    assert dup["accepted"] is False and dup["rejected"] is True
    assert dup["active_job_id"] == first["job_id"]
    gate.set()
    runner.wait_idle(timeout=3.0)
    runner.shutdown()


def test_runner_snapshot_has_contract_keys() -> None:
    runner, gate, _ = _gated_runner()
    gate.set()  # let it finish immediately
    job = runner.submit(JobSpec(tenant_id="t-a", path="/d"))
    assert runner.wait_idle(timeout=3.0)
    snap = runner.store.get(job["job_id"]).snapshot()
    for key in ("status", "phase", "counts", "percent", "queue_position", "started_at",
                "finished_at", "error", "embedding_model", "embedding_dim", "files_missing",
                "empty_graph", "summary"):
        assert key in snap
    assert snap["status"] == STATUS_SUCCEEDED
    assert snap["phase"] == PHASE_DONE and snap["percent"] == 100
    for key in ("objects", "nodes", "edges", "routines", "chunks"):
        assert key in snap["counts"]
    runner.shutdown()


def test_runner_classifies_warning_and_failure() -> None:
    # warning: execute returns empty_graph
    r1 = BaselineRunner(JobStore(),
                        execute=lambda job, on_progress: {"empty_graph": True, "files_missing": True},
                        classify=final_status)
    j1 = r1.submit(JobSpec(tenant_id="t-a", path="/d"))
    assert r1.wait_idle(timeout=3.0)
    snap = r1.store.get(j1["job_id"]).snapshot()
    assert snap["status"] == STATUS_WARNING and snap["files_missing"] is True
    r1.shutdown()

    # failure: execute raises
    def boom(job, on_progress):
        raise RuntimeError("mount blew up")

    r2 = BaselineRunner(JobStore(), execute=boom, classify=final_status)
    j2 = r2.submit(JobSpec(tenant_id="t-b", path="/d"))
    assert r2.wait_idle(timeout=3.0)
    snap2 = r2.store.get(j2["job_id"]).snapshot()
    assert snap2["status"] == STATUS_FAILED and "mount blew up" in snap2["error"]
    r2.shutdown()
