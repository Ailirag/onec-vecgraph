"""Opt-in web admin for the lite server: status page + workspace path configuration.

Pure parse/persist/render helpers (no HTTP here) so everything unit-tests without a
server — the thin Starlette routes live in `lite.server` (GET/POST /admin, /admin.json),
mirroring the baseline-jobs dashboard pattern. The admin only re-points the in-process
workspace at local metadata paths and is unauthenticated by design: keep it on loopback
(the default bind) or behind an authenticating proxy.

Applied paths are persisted to a small JSON state file (env ONEC_LITE_STATE, default
~/.onec-lite/config.json) so the next `serve-lite` starts with the same workspace even
without --root."""

from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path
from typing import Any

from .platform_help import render_help_lines


def state_file() -> Path:
    env = os.environ.get("ONEC_LITE_STATE", "").strip()
    return Path(env) if env else Path.home() / ".onec-lite" / "config.json"


def load_state(path: Path) -> dict:
    """Whole saved state ({} when absent/бит): root, ext_roots, platform_help entries."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_paths(path: Path) -> tuple[str, list[str]] | None:
    """Saved (root, ext_roots) or None when the state file is absent/бит/без корня."""
    data = load_state(path)
    root = str(data.get("root") or "").strip()
    ext = [str(p).strip() for p in (data.get("ext_roots") or []) if str(p).strip()]
    return (root, ext) if root else None


def load_help_entries(path: Path) -> list[dict]:
    """Saved platform-help entries [{'version','path'}] (empty when none)."""
    out = []
    for e in load_state(path).get("platform_help") or []:
        if isinstance(e, dict) and str(e.get("path") or "").strip():
            out.append({"version": str(e.get("version") or "").strip(),
                        "path": str(e["path"]).strip()})
    return out


def save_paths(path: Path, root: str, ext_roots: list[str],
               platform_help: list[dict] | None = None,
               rg_path: str | None = None) -> None:
    """Persist state; None for platform_help/rg_path keeps whatever the file already has."""
    saved = load_state(path)
    if platform_help is None:
        platform_help = saved.get("platform_help") or []
    if rg_path is None:
        rg_path = str(saved.get("rg_path") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"root": root, "ext_roots": ext_roots, "platform_help": platform_help,
                    "rg_path": rg_path},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def parse_ext_roots(text: str) -> list[str]:
    """Textarea/env form: one path per line, ';'-separated also accepted, quotes stripped."""
    out: list[str] = []
    for line in (text or "").replace(";", "\n").splitlines():
        line = line.strip().strip('"').strip()
        if line:
            out.append(line)
    return out


def workspace_snapshot(ws: Any) -> dict:
    """JSON-ready state of the current workspace (None -> unconfigured)."""
    if ws is None:
        return {"configured": False, "root": "", "ext_roots": [], "sources": []}
    return {
        "configured": True,
        "root": str(ws.root),
        "ext_roots": list(getattr(ws, "ext_roots", ())),
        "sources": [
            {
                "source": s.name,
                "format": s.fmt,
                "is_extension": s.is_extension,
                "root": str(s.root),
                "objects": sum(ws.kind_counts(s).values()),
            }
            for s in ws.sources
        ],
    }


def _help_rows(versions: list[dict], indexed: bool) -> str:
    if not versions:
        return '<tr><td colspan="3" class="empty">Справка не настроена — добавьте пути ниже.</td></tr>'
    rows = []
    for v in versions:
        if indexed:
            kinds = ", ".join(f"{k}: {n}" for k, n in sorted((v.get("by_help_kind") or {}).items()))
            topics = f"{v.get('topics', 0)} <span class=\"small\">({escape(kinds)})</span>"
        else:
            topics = '<span class="small">индекс не построен — «Построить индексы» или первый запрос</span>'
        rows.append(
            "<tr>"
            f"<td class=\"mono\">{escape(str(v.get('platform_version', '')))}</td>"
            f"<td class=\"num\">{escape(str(v.get('files', '')))}</td>"
            f"<td>{topics}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _fts_status_line(fts: dict) -> str:
    if not fts.get("available"):
        return '<span class="badge warn">FTS5 недоступен в этой сборке Python</span>'
    if not fts.get("built"):
        return "не построен — кнопка «Построить индекс поиска» ниже"
    size_mb = round((fts.get("size_bytes") or 0) / 1048576, 1)
    return (f"построен {escape(str(fts.get('built_at') or ''))} · юнитов "
            f"{fts.get('units', 0)} · файлов {fts.get('files', 0)} · {size_mb} МБ · "
            "дообновляется по mtime при поиске")


def _source_rows(sources: list[dict]) -> str:
    if not sources:
        return '<tr><td colspan="4" class="empty">Источники не загружены — задайте пути ниже.</td></tr>'
    rows = []
    for s in sources:
        tag = "расширение" if s.get("is_extension") else "база"
        rows.append(
            "<tr>"
            f"<td class=\"mono\">{escape(str(s.get('source', '')))}</td>"
            f"<td>{escape(str(s.get('format', '')))} · {tag}</td>"
            f"<td class=\"num\">{escape(str(s.get('objects', '')))}</td>"
            f"<td class=\"mono small\">{escape(str(s.get('root', '')))}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_admin_page(
    snap: dict, *, rg: str | None, state_path: str, message: str = "", error: str = ""
) -> str:
    """The full admin page: status, sources table, path form (root + extensions)."""
    configured = bool(snap.get("configured"))
    status = (
        '<span class="badge ok">настроен</span>'
        if configured
        else '<span class="badge warn">не настроен</span>'
    )
    rg_note = (
        f'<span class="mono small">{escape(rg)}</span>'
        if rg
        else '<span class="badge warn">не найден — поиск на Python-фолбэке (медленно)</span>'
    )
    banner = ""
    if message:
        banner = f'<div class="banner ok-b">{escape(message)}</div>'
    elif error:
        banner = f'<div class="banner err-b">{escape(error)}</div>'
    ext_text = "\n".join(snap.get("ext_roots") or [])
    help_snap = snap.get("platform_help") or {}
    help_text = render_help_lines(help_snap.get("entries") or [])
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>onec-lite — админка</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
          margin: 1.5rem auto; max-width: 60rem; padding: 0 1rem; color: #111827; background: #f9fafb; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .25rem; }}
  h2 {{ font-size: .95rem; margin: 1.4rem 0 .5rem; }}
  .meta {{ color: #6b7280; font-size: .8rem; margin-bottom: 1rem; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff;
           box-shadow: 0 1px 2px rgba(0,0,0,.06); border-radius: 8px; overflow: hidden; }}
  th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #f0f0f0;
            font-size: .85rem; vertical-align: top; }}
  th {{ background: #f3f4f6; font-weight: 600; font-size: .72rem; text-transform: uppercase;
        letter-spacing: .03em; color: #6b7280; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .small {{ font-size: .76rem; color: #4b5563; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .empty {{ color: #9ca3af; text-align: center; padding: 1.2rem; }}
  .badge {{ display: inline-block; padding: .08rem .5rem; border-radius: 999px;
            font-size: .74rem; font-weight: 600; }}
  .ok {{ background: #dcfce7; color: #166534; }}
  .warn {{ background: #fef3c7; color: #92400e; }}
  .banner {{ padding: .5rem .8rem; border-radius: 8px; font-size: .85rem; margin-bottom: 1rem; }}
  .ok-b {{ background: #dcfce7; color: #166534; }}
  .err-b {{ background: #fee2e2; color: #991b1b; }}
  form {{ background: #fff; padding: 1rem; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,.06); }}
  label {{ display: block; font-size: .78rem; font-weight: 600; color: #374151; margin: .6rem 0 .25rem; }}
  input[type=text], textarea {{ width: 100%; box-sizing: border-box; padding: .45rem .55rem;
    border: 1px solid #d1d5db; border-radius: 6px; font-family: ui-monospace, Menlo, monospace;
    font-size: .82rem; }}
  textarea {{ min-height: 4.5rem; resize: vertical; }}
  .hint {{ color: #6b7280; font-size: .74rem; margin-top: .2rem; }}
  .actions {{ margin-top: .9rem; display: flex; gap: .6rem; }}
  button {{ padding: .45rem .9rem; border: 0; border-radius: 6px; font-weight: 600;
            font-size: .82rem; cursor: pointer; }}
  .primary {{ background: #3b82f6; color: #fff; }}
  .secondary {{ background: #e5e7eb; color: #374151; }}
</style></head>
<body>
  <h1>onec-lite — админка</h1>
  <div class="meta">рабочая копия: {status} · ripgrep: {rg_note}</div>
  {banner}
  <h2>Источники (порядок разрешения: расширения → база)</h2>
  <table>
    <thead><tr><th>источник</th><th>формат</th><th>объектов</th><th>каталог</th></tr></thead>
    <tbody>
{_source_rows(snap.get("sources") or [])}
    </tbody>
  </table>
  <h2>Пути рабочей копии</h2>
  <form method="post" action="admin">
    <label for="root">Корень конфигурации (выгрузка Конфигуратора или EDT-воркспейс)</label>
    <input type="text" id="root" name="root" value="{escape(snap.get("root") or "")}"
           placeholder="H:\\path\\to\\ut  или  D:\\dumps\\erp_xml">
    <div class="hint">EDT-воркспейс: каталог с проектами (база + расширения находятся автоматически).</div>
    <label for="ext_roots">Дополнительные корни расширений (по одному пути в строке; обычно не нужно)</label>
    <textarea id="ext_roots" name="ext_roots"
              placeholder="D:\\ext\\ДИТ_Расширение">{escape(ext_text)}</textarea>
    <h2>Справка платформы (.hbk)</h2>
    <table>
      <thead><tr><th>версия платформы</th><th>файлов .hbk</th><th>тем</th></tr></thead>
      <tbody>
{_help_rows(help_snap.get("versions") or [], bool(help_snap.get("indexed")))}
      </tbody>
    </table>
    <label for="help_paths">Пути к справке: каталог bin платформы или файл .hbk
      (по одному в строке; «версия = путь» задаёт версию явно, иначе она берётся из пути)</label>
    <textarea id="help_paths" name="help_paths"
              placeholder="C:\\Program Files\\1cv8\\8.3.27.2130\\bin&#10;8.3.18 = D:\\help\\shcntx_ru.hbk">{escape(help_text)}</textarea>
    <h2>Поиск</h2>
    <div class="hint" style="margin-bottom:.4rem">Ранжированный индекс (SQLite FTS5, BM25):
      {_fts_status_line(snap.get("fts") or {})}</div>
    <label for="rg_path">Путь к ripgrep (rg.exe) — пусто = автопоиск: PATH → WinGet → VS Code</label>
    <input type="text" id="rg_path" name="rg_path" value="{escape(snap.get("rg_override") or "")}"
           placeholder="C:\\tools\\ripgrep\\rg.exe">
    <div class="hint">Без ripgrep поиск работает на Python-фолбэке (в десятки раз медленнее).
      Установка: <span class="mono">winget install BurntSushi.ripgrep.MSVC</span></div>
    <div class="actions">
      <button class="primary" type="submit" name="action" value="apply">Применить и сохранить</button>
      <button class="secondary" type="submit" name="action" value="build_help"
              title="Разобрать все .hbk и построить индекс имён (десятки секунд на версию)">Построить индексы справки</button>
      <button class="secondary" type="submit" name="action" value="build_fts"
              title="Проиндексировать рутины и карточки объектов для fts_search (первый раз — минуты; дальше инкрементально по mtime)">Построить индекс поиска</button>
      <button class="secondary" type="submit" name="action" value="refresh">Сбросить кэши</button>
    </div>
  </form>
  <div class="meta" style="margin-top:1rem">
    состояние сохраняется в <span class="mono">{escape(state_path)}</span> ·
    <a href="admin.json">admin.json</a> · MCP-эндпоинт: <span class="mono">/mcp</span>
  </div>
</body></html>"""
