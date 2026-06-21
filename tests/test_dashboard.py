"""Unit tests for the read-only baseline-jobs dashboard (pure render functions + store ordering)."""

from __future__ import annotations

from onec_vecgraph.dashboard import render_page, render_rows
from onec_vecgraph.jobs import BaselineJob, JobStore


def _snap(**over):
    job = BaselineJob(job_id="bl-1", tenant_id="grand@release", **over)
    return job.snapshot()


def test_render_rows_empty_state() -> None:
    html = render_rows([])
    assert "Пока нет" in html and "<tr>" in html


def test_render_rows_includes_core_fields() -> None:
    snap = _snap(status="running", phase="vectorize", percent=66)
    snap["counts"].update({"objects": 1200, "nodes": 5000})
    html = render_rows([snap])
    assert "bl-1" in html and "grand@release" in html
    assert "running" in html and "vectorize" in html
    assert "66%" in html and "obj:1200" in html and "nodes:5000" in html


def test_render_rows_shows_warning_flags() -> None:
    html = render_rows([_snap(status="warning", files_missing=True, empty_graph=True)])
    assert "files_missing" in html and "empty_graph" in html


def test_render_rows_shows_queue_position_for_queued() -> None:
    html = render_rows([_snap(status="queued", queue_position=2)])
    assert "#2" in html


def test_render_rows_escapes_dynamic_text() -> None:
    # An error string with HTML must be escaped (no raw injection into the page).
    html = render_rows([_snap(status="failed", error="<script>boom</script>")])
    assert "<script>boom" not in html
    assert "&lt;script&gt;boom" in html


def test_render_page_has_shell_rows_and_refresh() -> None:
    page = render_page([_snap(status="succeeded")], generated_at="2026-06-21T08:00:00+00:00", active=0)
    assert page.startswith("<!doctype html>")
    assert "baseline jobs" in page
    assert 'id="jobs-body"' in page and "bl-1" in page          # rows embedded for first paint
    assert "setInterval" in page and "partial=1" in page         # live refresh wiring
    assert "2026-06-21T08:00:00+00:00" in page


def test_job_store_list_all_newest_first() -> None:
    store = JobStore()
    store.add(BaselineJob(job_id="old", tenant_id="t", created_at="2026-01-01T00:00:00+00:00"))
    store.add(BaselineJob(job_id="new", tenant_id="t", created_at="2026-01-02T00:00:00+00:00"))
    order = [j.job_id for j in store.list_all()]
    assert order == ["new", "old"]
