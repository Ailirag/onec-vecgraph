"""Read-only HTML dashboard for baseline jobs (served by the admin server at GET /jobs).

Pure render functions over job snapshots (see :meth:`jobs.BaselineJob.snapshot`) — no I/O, so they
unit-test without a server. The full page embeds the same row HTML it polls for: a tiny script
re-fetches ``?partial=1`` (which returns just the table body) and swaps it in, so the table updates
live without a full-page reload. No external assets (CSP-safe, works offline)."""

from __future__ import annotations

from html import escape
from typing import Any

# status → (background, text) badge colours.
_STATUS_COLOURS = {
    "queued": ("#e5e7eb", "#374151"),
    "running": ("#dbeafe", "#1e40af"),
    "succeeded": ("#dcfce7", "#166534"),
    "warning": ("#fef3c7", "#92400e"),
    "failed": ("#fee2e2", "#991b1b"),
}
_REFRESH_MS = 2000


def _txt(v: Any) -> str:
    return "" if v is None else escape(str(v))


def _badge(status: str) -> str:
    bg, fg = _STATUS_COLOURS.get(status, ("#e5e7eb", "#374151"))
    return (f'<span class="badge" style="background:{bg};color:{fg}">{escape(status)}</span>')


def _flags(snap: dict[str, Any]) -> str:
    out = []
    if snap.get("files_missing"):
        out.append('<span class="flag flag-bad" title="dump path missing/empty inside the container">files_missing</span>')
    if snap.get("empty_graph"):
        out.append('<span class="flag flag-bad" title="indexed but produced 0 objects">empty_graph</span>')
    return " ".join(out)


def _short_ts(v: Any) -> str:
    # ISO '2026-06-21T08:00:00+00:00' → 'HH:MM:SS' for compactness; fall back to raw.
    s = _txt(v)
    return escape(s[11:19]) if len(s) >= 19 and s[10] == "T" else s


def render_rows(snapshots: list[dict[str, Any]]) -> str:
    """The `<tbody>` inner HTML (also what `?partial=1` returns for live refresh)."""
    if not snapshots:
        return '<tr><td colspan="9" class="empty">Пока нет ни одной baseline-джобы.</td></tr>'
    rows = []
    for s in snapshots:
        c = s.get("counts") or {}
        counts = " · ".join(
            f"{label}:{_txt(c.get(key))}" for label, key in
            (("obj", "objects"), ("nodes", "nodes"), ("edges", "edges"),
             ("rout", "routines"), ("chunks", "chunks"))
            if c.get(key) is not None
        ) or "—"
        pct = int(s.get("percent") or 0)
        queued_note = ""
        if s.get("status") == "queued" and (s.get("queue_position") or 0) > 0:
            queued_note = f' <span class="qpos">#{int(s["queue_position"])}</span>'
        rows.append(
            "<tr>"
            f'<td class="mono">{_txt(s.get("job_id"))}</td>'
            f'<td class="mono">{_txt(s.get("tenant_id"))}</td>'
            f"<td>{_badge(str(s.get('status', '')))}{queued_note}</td>"
            f"<td>{_txt(s.get('phase'))}</td>"
            f'<td class="pcell"><div class="bar"><div class="fill" style="width:{pct}%"></div></div>'
            f'<span class="pct">{pct}%</span></td>'
            f'<td class="mono small">{counts}</td>'
            f"<td class=\"mono small\">{_short_ts(s.get('started_at'))}→{_short_ts(s.get('finished_at'))}</td>"
            f"<td>{_flags(s)}</td>"
            f'<td class="err">{_txt(s.get("error"))}</td>'
            "</tr>"
        )
    return "\n".join(rows)


def render_page(snapshots: list[dict[str, Any]], *, generated_at: str, active: int,
                refresh_ms: int = _REFRESH_MS) -> str:
    """The full dashboard page. Polls `?partial=1` every `refresh_ms` to refresh the table in place."""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>onec-vecgraph — baseline jobs</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
          margin: 1.5rem; color: #111827; background: #f9fafb; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .25rem; }}
  .meta {{ color: #6b7280; font-size: .8rem; margin-bottom: 1rem; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.06);
           border-radius: 8px; overflow: hidden; }}
  th, td {{ text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #f0f0f0; font-size: .85rem;
            vertical-align: top; }}
  th {{ background: #f3f4f6; font-weight: 600; font-size: .72rem; text-transform: uppercase;
        letter-spacing: .03em; color: #6b7280; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .small {{ font-size: .76rem; color: #4b5563; }}
  .badge {{ display: inline-block; padding: .08rem .45rem; border-radius: 999px; font-size: .72rem;
            font-weight: 600; }}
  .qpos {{ color: #6b7280; font-size: .72rem; }}
  .pcell {{ white-space: nowrap; }}
  .bar {{ display: inline-block; width: 70px; height: 7px; background: #e5e7eb; border-radius: 4px;
          overflow: hidden; vertical-align: middle; }}
  .fill {{ height: 100%; background: #3b82f6; }}
  .pct {{ font-size: .72rem; color: #6b7280; margin-left: .35rem; }}
  .flag {{ display: inline-block; padding: .05rem .4rem; border-radius: 4px; font-size: .7rem;
           font-weight: 600; }}
  .flag-bad {{ background: #fee2e2; color: #991b1b; }}
  .err {{ color: #b91c1c; font-size: .76rem; max-width: 22rem; word-break: break-word; }}
  .empty {{ color: #9ca3af; text-align: center; padding: 1.5rem; }}
</style></head>
<body>
  <h1>onec-vecgraph — baseline jobs</h1>
  <div class="meta">read-only · активных: <b id="active">{active}</b> ·
    обновлено <span id="ts">{escape(generated_at)}</span> · автообновление {refresh_ms // 1000}s</div>
  <table>
    <thead><tr>
      <th>job_id</th><th>tenant</th><th>status</th><th>phase</th><th>progress</th>
      <th>counts</th><th>started→finished</th><th>flags</th><th>error</th>
    </tr></thead>
    <tbody id="jobs-body">
{render_rows(snapshots)}
    </tbody>
  </table>
<script>
  const REFRESH = {refresh_ms};
  async function tick() {{
    try {{
      const r = await fetch(window.location.pathname + '?partial=1', {{ cache: 'no-store' }});
      if (r.ok) {{
        document.getElementById('jobs-body').innerHTML = await r.text();
        document.getElementById('ts').textContent = new Date().toLocaleTimeString();
      }}
    }} catch (e) {{ /* keep last good render on transient errors */ }}
  }}
  setInterval(tick, REFRESH);
</script>
</body></html>"""
