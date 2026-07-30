"""onec-lite — zero-infrastructure MCP server over a live 1C working copy.

Reads a Configurator XML dump or a 1C:EDT workspace (base + extensions) directly from
disk: no Neo4j, no embeddings, results always match the current files. Search is
ripgrep-accelerated; code answers are verified by the project's BSL parser.

Start via CLI: `onec-vecgraph serve-lite --root <путь>` (stdio by default).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from ..chunking import KIND_RU
from ..parsing.dump import CODE_FOLDERS, TYPE_FOLDERS
from ..parsing.model import MetaObject
from . import admin as lite_admin
from . import code_intel, fts, gitops, gitview, metaview, platform_help, search
from .workspace import Workspace, read_text

INSTRUCTIONS = """onec-lite: навигация по ЖИВОЙ рабочей копии конфигурации 1С (база + расширения),
без векторизации — данные всегда соответствуют текущим файлам на диске.

Словарь: kind — вид метаданных (Catalog, Document, CommonModule, ...); module — псевдоним
модуля (Module|Object|Manager|RecordSet|Value|Command|Form:<Имя>|<имя файла .bsl>);
source — имя источника из overview() (пусто = все, расширения раньше базы);
workspace — рабочая копия из list_workspaces() (сервер держит несколько репозиториев 1С).
Дефолт задаёт ПОДКЛЮЧЕНИЕ: project-scope .mcp.json проекта шлёт заголовок X-Workspace — на
эту конфигурацию и работай, НЕ указывая workspace. Аргумент workspace=<имя> в вызове передавай
ТОЛЬКО когда пользователь ЯВНО просит другую конфигурацию (или сравнить с ней) — иначе опускай.
Полный приоритет: аргумент workspace → заголовок X-Workspace/X-Tenant-Id → env
ONEC_LITE_WORKSPACE → активный из админки.

ЧЕМ ПОЛЬЗОВАТЬСЯ. Эти инструменты НЕ заменяют Grep/ripgrep — они отвечают на вопросы, которые
поиском по тексту выразить нельзя. Замеры на конфигурации в 15 тыс. модулей: по токенам обычный
grep дешевле в 2-5 раз (а на агрегатах через конвейер — в 10-25 раз), потому что отдаёт
`путь:строка:текст` без обвязки; выигрыш здешних инструментов — в ПРОВЕРЕННОСТИ и ПОЛНОТЕ ответа.

Бери Grep/ripgrep, когда вопрос — «где встречается такой текст»: подстрока, regex, литерал,
имя в комментарии; когда нужен дешёвый счёт/агрегат (`rg -c ... | sort | uniq -c`); когда надо
прочитать известный файл или окно строк; для git — `git diff/status/log` (они дешевле и быстрее
здешних changed_objects/review_set); и вообще на первом, разведочном вопросе.

Бери инструменты отсюда, когда ответ требует РАЗБОРА кода или метаданных 1С:
* вызов это или объявление — find_callers/find_callees/call_graph (у наивного grep на популярном
  методе почти вся выдача оказывается объявлениями, плюс попадания из комментариев);
* какому объекту принадлежит перехват расширения — find_overrides (текстом `&Вместо` найдётся,
  но связать его с целевым объектом нельзя);
* полный состав реквизитов с типами и обязательностью, движения документа, владельцы —
  get_object/writes_to: заимствованный объект лежит в расширении ОГРЫЗКОМ, и правильный ответ
  требует слияния базы с расширениями (grep покажет 2-3 копии и оставит выбор тебе);
* границы рутины и её тело — read_routine (модули 1С не влезают в лимит чтения целиком);
* «где считается X» человеческим языком — fts_search (ранжирование BM25 ставит нужный метод в
  начало; grep выдаёт сотни совпадений без порядка);
* сколько ВСЕГО вызовов/использований/объявлений — здесь есть полные счётчики
  (call_rows_total/usage_count/declaration_count), grep обязан либо усечь, либо доплатить
  полным проходом;
* модули приложения и сеанса (папка Configuration), точки входа, обработчики — find_routine/
  find_handlers/list_routines(kind="Configuration").

Куда идти: обзор -> overview/metrics; структура -> list_objects/get_object;
зависимости -> get_dependencies (связи объекта) / find_type_usages (где используется тип);
код -> list_routines/read_module/read_routine; поиск -> fts_search (ранжированный BM25,
лучший старт для «где считается X») / search_code (точная подстрока/regex — здесь Grep обычно
дешевле) / search_metadata / find_routine;
анализ -> find_callers/find_callees/call_graph/find_handlers/find_overrides/writes_to;
изменения ветки (git) -> changed_objects (что поменялось) / review_set (затронутые
рутины + их вызывающие — ревью-набор незакоммиченной работы; сам дифф дешевле смотреть git'ом);
UI и интеграции -> get_form (структура формы) / get_service (HTTP- и Web-сервисы);
справка платформы (синтаксис-помощник) -> platform_docinfo/platform_search/
platform_get_document/platform_versions (пути к .hbk задаются в админке /admin)."""

def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


# Own host/port envs (not the big server's MCP_PORT): the docker read-MCP holds :8000,
# lite must not collide with it out of the box when started with --transport http.
mcp = FastMCP(
    "onec-lite",
    instructions=INSTRUCTIONS,
    host=_env("ONEC_LITE_HOST", "127.0.0.1"),
    port=int(_env("ONEC_LITE_PORT", "8010")),
    streamable_http_path="/mcp",
    stateless_http=True,
)

# Named workspaces: one lite process can hold several 1C repositories at once.
# Sessions pin their default via env ONEC_LITE_WORKSPACE; every tool accepts an
# explicit `workspace` argument that overrides it (see default_workspace_name()).
_WORKSPACES: dict[str, Workspace] = {}
_RG_INIT = False


def _init_rg_from_state() -> None:
    """Apply the ripgrep path saved by the admin (once; env/явный set_rg_path сильнее)."""
    global _RG_INIT
    if _RG_INIT:
        return
    _RG_INIT = True
    saved = str(lite_admin.load_state(lite_admin.state_file()).get("rg_path") or "").strip()
    if saved and not search.rg_override():
        search.set_rg_path(saved)


def default_workspace_name() -> str:
    """Process default: env ONEC_LITE_WORKSPACE → saved active → единственный → 'default'.

    The env step is what makes different Claude Code sessions independent: each stdio
    process pins its own default, the shared `active` in the state file is only a
    fallback for single-workspace setups."""
    env = os.environ.get("ONEC_LITE_WORKSPACE", "").strip()
    if env:
        return env
    wss, active = lite_admin.load_workspaces(lite_admin.state_file())
    if active:
        return active
    if len(wss) == 1:
        return next(iter(wss))
    if len(_WORKSPACES) == 1:
        return next(iter(_WORKSPACES))
    return "default"


def configure(root: str | Path, ext_roots: tuple[str | Path, ...] = (),
              name: str = "default") -> Workspace:
    """Build a workspace and register it under `name` (CLI startup / admin apply / lazy).

    Registration happens only after Workspace() succeeds, so a bad path keeps serving
    the previous workspace. Code-intel caches are dropped: source names may stay the
    same while pointing at a different checkout."""
    _init_rg_from_state()
    ws = Workspace(root, ext_roots)
    _WORKSPACES[name] = ws
    code_intel.clear_caches()
    return ws


# Последний результат обновления из remote на воркспейс (админка/диагностика).
_UPDATE_RESULTS: dict[str, dict] = {}


def _entry_root(name: str, entry: dict) -> Path:
    """Каталог воркспейса: явный root или управляемое зеркало ~/.onec-lite/mirrors/<имя>."""
    root = str(entry.get("root") or "").strip()
    return Path(root) if root else gitops.mirror_path(name)


def _maybe_update_on_start(name: str, entry: dict) -> None:
    """Per-workspace update_on_start (off|fetch|pull) — один раз, при ленивом построении.

    Ошибки не блокируют воркспейс (обслуживаем то, что на диске) и не пишут в stdout
    (он принадлежит MCP-протоколу) — итог виден в админке и в логе."""
    mode = str(entry.get("update_on_start") or "off")
    if mode not in ("fetch", "pull"):
        return
    if entry.get("repo") and not (gitops.mirror_path(name) / ".git").is_dir():
        _UPDATE_RESULTS[name] = {"ok": False, "op": "start",
                                 "error": "зеркало ещё не клонировано — кнопка «Обновить» в админке"}
        return
    res = gitops.update_workspace(name, entry, mode=mode)
    res["trigger"] = "on_start"
    _UPDATE_RESULTS[name] = res
    logging.getLogger(__name__).info("workspace %s update_on_start(%s): %s",
                                     name, mode, res.get("output") or res.get("error"))


def _fts_autobuild_enabled() -> bool:
    return os.environ.get("ONEC_LITE_FTS_AUTOBUILD", "").strip().lower() not in (
        "off", "0", "false", "no")


def _maybe_build_fts(ws: Workspace) -> None:
    """Фоновый прогрев FTS-индекса при загрузке воркспейса (неблокирующе, идемпотентно) —
    чтобы к первому fts_search индекс был готов или уже строился. Отключить:
    ONEC_LITE_FTS_AUTOBUILD=off."""
    if not _fts_autobuild_enabled():
        return
    try:
        fts.index_for(ws).ensure_background(force=True)
    except Exception:  # noqa: BLE001 — прогрев не должен ронять загрузку воркспейса
        logging.getLogger(__name__).exception("fts autobuild kick failed")


def _header_workspace_names() -> list[str]:
    """HTTP-заголовки с именем воркспейса, в порядке приоритета (env-настраиваемо).

    Дефолт: X-Workspace, затем X-Tenant-Id — тенант-заголовок оркестратора, которым
    он делит проекты (см. serve-lite мультипроект). Переопределить набор/имена —
    ONEC_LITE_WORKSPACE_HEADER (список через запятую или пробел)."""
    raw = os.environ.get("ONEC_LITE_WORKSPACE_HEADER", "")
    names = [h.strip() for h in raw.replace(",", " ").split() if h.strip()]
    return names or ["X-Workspace", "X-Tenant-Id"]


def _request_headers():
    """Заголовки текущего MCP-запроса (Starlette Headers) или None — вне запроса / в stdio.

    FastMCP пробрасывает Request в контекст только для streamable-http; в stdio (и когда
    тул вызван напрямую, вне запроса) Request отсутствует, поэтому выбор воркспейса по
    заголовку молча пропускается и берётся дефолт процесса."""
    try:
        request = mcp.get_context().request_context.request
    except Exception:  # noqa: BLE001 — вне запроса / stdio / контекст недоступен
        return None
    return getattr(request, "headers", None)


def _workspace_from_headers() -> str:
    """Имя воркспейса из заголовка запроса (HTTP-мультипроект) или '' если заголовка нет.

    Позволяет одному http serve-lite обслуживать несколько проектов оркестратора: клиент
    шлёт X-Workspace (или тенант-заголовок X-Tenant-Id) при каждом вызове. Имена заголовков —
    из _header_workspace_names(); значение = имя из list_workspaces(). В stdio → ''."""
    headers = _request_headers()
    if headers is None:
        return ""
    for name in _header_workspace_names():
        value = (headers.get(name) or "").strip()
        if value:
            return value
    return ""


def _resolve_ws_name(workspace: str = "") -> str:
    """Эффективное имя воркспейса для вызова: явный аргумент → заголовок запроса → дефолт.

    Единый порядок резолва для _ws() и для полей ответа (overview), чтобы отчёт о
    воркспейсе совпадал с тем, что реально обслуживалось."""
    return (workspace or "").strip() or _workspace_from_headers() or default_workspace_name()


def _ws(workspace: str = "") -> Workspace:
    """Workspace by name; пусто = заголовок запроса (http) → дефолт процесса. Lazy-builds.

    Приоритет резолва: явный аргумент `workspace` > HTTP-заголовок X-Workspace/X-Tenant-Id
    (только streamable-http, см. _workspace_from_headers) > дефолт процесса
    (default_workspace_name). В stdio заголовков нет → поведение прежнее."""
    name = _resolve_ws_name(workspace)
    ws = _WORKSPACES.get(name)
    if ws is not None:
        return ws
    wss, _active = lite_admin.load_workspaces(lite_admin.state_file())
    entry = wss.get(name)
    if entry is None and name == default_workspace_name():
        # Legacy/env path: ONEC_LITE_ROOT binds to the process-default name.
        root = os.environ.get("ONEC_LITE_ROOT", "").strip()
        if root:
            ext = tuple(lite_admin.parse_ext_roots(os.environ.get("ONEC_LITE_EXT_ROOTS", "")))
            ws = configure(root, ext, name=name)
            _maybe_build_fts(ws)
            return ws
    if entry is None:
        known = ", ".join(sorted(wss)) or "(нет ни одного)"
        raise RuntimeError(
            f"Воркспейс '{name}' не сконфигурирован. Известные: {known}. "
            "Задайте --root/ONEC_LITE_ROOT, --workspace или добавьте в админке (/admin)."
        )
    root = _entry_root(name, entry)
    if entry.get("repo") and not root.is_dir():
        raise RuntimeError(
            f"Зеркало воркспейса '{name}' ещё не клонировано ({entry['repo']}). "
            "Клонировать: кнопка «Обновить из remote» в админке или `onec-lite update "
            f"--workspace {name}`."
        )
    _maybe_update_on_start(name, entry)
    ws = configure(root, tuple(entry["ext_roots"]), name=name)
    _maybe_build_fts(ws)
    return ws


_HELP = platform_help.HelpCatalog()
_HELP_INIT = False


def configure_help(entries: list[dict]) -> list[str]:
    """Swap the platform-help config (CLI startup / admin apply); returns per-entry errors."""
    global _HELP_INIT
    _HELP_INIT = True
    return _HELP.configure(entries)


def _help() -> platform_help.HelpCatalog:
    """Catalog with lazy first-use config: env ONEC_LITE_HELP, затем сохранённое состояние."""
    global _HELP_INIT
    if not _HELP_INIT:
        _HELP_INIT = True
        entries = platform_help.parse_help_lines(os.environ.get("ONEC_LITE_HELP", ""))
        if not entries:
            entries = lite_admin.load_help_entries(lite_admin.state_file())
        if entries:
            _HELP.configure(entries)
    return _HELP


def help_catalog() -> platform_help.HelpCatalog:
    """Public accessor for the CLI (--check) and tests."""
    return _help()


def _err(msg: str) -> dict:
    return {"error": msg}


def _kind_ok(kind: str) -> str | None:  # noqa: D401
    # CODE_FOLDERS, а не TYPE_FOLDERS: иначе kind="Configuration" отвергался, и модули
    # приложения/сеанса были ВИДИМЫ поиску, но нечитаемы (read_routine/list_routines/read_module
    # отвечали «неизвестный вид»), хотя find_routine их уже находил.
    if kind in set(CODE_FOLDERS.values()):
        return None
    near = ", ".join(sorted(k for k in CODE_FOLDERS.values() if kind.lower() in k.lower())[:5])
    return f"Неизвестный вид метаданных '{kind}'." + (f" Похожие: {near}." if near else "")


# --------------------------------------------------------------------------- #
# Обзор / структура
# --------------------------------------------------------------------------- #

@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def overview(workspace: str = "") -> dict:
    """Обзор рабочей копии: источники (база + расширения) и число объектов по видам.

    workspace — имя из list_workspaces(); пусто = дефолт сессии."""
    ws = _ws(workspace)
    return {
        "workspace": _resolve_ws_name(workspace),
        "root": str(ws.root),
        "sources": [
            {
                "source": s.name,
                "format": s.fmt,
                "is_extension": s.is_extension,
                "config_id": s.part.config_id,
                "purpose": s.part.purpose,
                "objects_by_kind": ws.kind_counts(s),
            }
            for s in ws.sources
        ],
        "resolution_order": [s.name for s in ws.sources],
        **({"unattached_sources": unattached,
            "warning": "Рядом с рабочей копией есть проекты 1С, НЕ подключённые к воркспейсу — "
                       "их код и изменения не попадают ни в один ответ (включая ревью). "
                       "Добавьте пути в «Корни расширений» в админке (/admin)."}
           if (unattached := _unattached_projects(ws)) else {}),
    }


def _unattached_projects(ws: Workspace) -> list[dict]:
    """Проекты 1С в дереве репозитория, которые НЕ входят в воркспейс.

    Молчаливая слепая зона: расширение лежит рядом с выгруженной конфигурацией, но не указано в
    ext_roots — его объекты не находятся, а его изменения отфильтровываются в changed_objects/
    review_set без единого предупреждения (на боевом УТ так пропадало расширение на 689 .bsl)."""
    known = {str(s.root).lower() for s in ws.sources}
    out: list[dict] = []
    try:
        from . import gitview as _gv
        roots = list(_gv._repos(ws.sources)[0]) or [ws.root]  # noqa: SLF001
    except Exception:  # noqa: BLE001
        roots = [ws.root]
    seen: set[str] = set()
    for repo in roots:
        try:
            candidates = [p for p in repo.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            continue
        for cand in candidates:
            key = str(cand).lower()
            if key in known or key in seen:
                continue
            seen.add(key)
            mdo = cand / "src" / "Configuration" / "Configuration.mdo"
            cfg_xml = cand / "Configuration.xml"
            if mdo.is_file() or cfg_xml.is_file():
                bsl = sum(1 for _ in cand.rglob("*.bsl"))
                out.append({"path": str(cand), "format": "edt" if mdo.is_file() else "configurator",
                            "bsl_files": bsl})
    return out


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def list_workspaces() -> dict:
    """Рабочие копии, которые знает сервер: имена, корни, активная и дефолт этой сессии.

    Любой инструмент принимает workspace=<имя>; пусто = default_workspace."""
    wss, active = lite_admin.load_workspaces(lite_admin.state_file())
    for name, ws in _WORKSPACES.items():  # сконфигурированные в процессе (env/--root)
        wss.setdefault(name, {"root": str(ws.root),
                              "ext_roots": [str(p) for p in ws.ext_roots]})
    default = default_workspace_name()
    return {
        "workspaces": [
            {"name": n, "root": e["root"], "ext_roots": e["ext_roots"],
             "active": n == active, "loaded": n in _WORKSPACES}
            for n, e in sorted(wss.items())
        ],
        "active": active,
        "default_workspace": default,
        "note": "workspace=<имя> в любом инструменте; пусто = default_workspace.",
    }


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def list_kinds() -> dict:
    """Все допустимые значения параметра kind (+ русские названия)."""
    kinds = sorted(set(TYPE_FOLDERS.values()))
    return {"kinds": kinds, "ru": {k: KIND_RU[k] for k in kinds if k in KIND_RU}}


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def list_objects(kind: str, filter: str = "", limit: int = 200, source: str = "", workspace: str = "") -> dict:
    """Объекты вида по всем источникам; filter — подстрока имени (без регистра).

    Совпадение имени в нескольких источниках отражается полем in_multiple_sources."""
    ws = _ws(workspace)
    if err := _kind_ok(kind):
        return _err(err)
    srcs, serr = ws.resolve_sources(source)
    if serr:
        return _err(serr)
    flt = filter.lower()
    seen: dict[str, list[str]] = {}
    order: list[str] = []
    for s in srcs:
        for fqn, _meta, _dir in ws.listing(s):
            k, _, name = fqn.partition(".")
            if k != kind or (flt and flt not in name.lower()):
                continue
            if name not in seen:
                order.append(name)
            seen.setdefault(name, []).append(s.name)
    truncated = len(order) > limit
    rows = [
        {"name": n, "source": seen[n][0],
         **({"in_multiple_sources": seen[n]} if len(seen[n]) > 1 else {})}
        for n in order[:limit]
    ]
    return {"kind": kind, "count": len(order), "truncated": truncated, "objects": rows}


def _object_payload(ws: Workspace, obj: MetaObject, detail: bool) -> dict:
    out: dict = {
        "fqn": obj.fqn,
        "kind": obj.kind,
        "kind_ru": KIND_RU.get(obj.kind),
        "name": obj.name,
        "synonym": obj.synonym,
        "comment": obj.comment,
        "config_id": obj.config_id,
        "belonging": obj.belonging,
    }
    if obj.flags:
        out["flags"] = obj.flags
    if obj.fields:
        out["attributes"] = [
            {"name": f.name, "synonym": f.synonym, "role": f.role, "type": f.type_text,
             **({"required": True} if f.fill_checking == "ShowError" else {})}
            for f in obj.fields[:100]
        ]
        if len(obj.fields) > 100:
            out["attributes_total"] = len(obj.fields)
    if obj.tabular:
        out["tabular_sections"] = [
            {
                "name": t.name,
                "synonym": t.synonym,
                **({"required": True} if t.fill_checking == "ShowError" else {}),
                "attributes": [
                    {"name": f.name, "synonym": f.synonym, "type": f.type_text,
                     **({"required": True} if f.fill_checking == "ShowError" else {})}
                    for f in t.fields[:60]
                ],
            }
            for t in obj.tabular
        ]
    if obj.enum_values:
        out["enum_values"] = [{"name": v.name, "synonym": v.synonym} for v in obj.enum_values]
    if obj.predefined:
        out["predefined"] = [
            {"name": p.name, "code": p.code, "is_folder": p.is_folder}
            for p in obj.predefined[:60]
        ]
    if obj.forms:
        out["forms"] = [
            {"name": f.name, "has_module": bool(f.module_path), "has_layout": bool(f.form_path)}
            for f in obj.forms
        ]
    if obj.modules:
        out["modules"] = [{"module": m.module_type, "size": m.size} for m in obj.modules]
    if obj.owners:
        out["owners"] = obj.owners
    if obj.register_records:
        out["register_records"] = obj.register_records
    if obj.content:
        out["subsystem_content"] = obj.content[:200]
    if obj.child_subsystems:
        out["child_subsystems"] = obj.child_subsystems
    if obj.event:
        out["event"] = obj.event
        out["handler"] = obj.handler_raw
        out["source_types"] = [t.raw for t in obj.source_types if getattr(t, "raw", "")] or [
            getattr(t, "ref_name", "") for t in obj.source_types
        ]
    if obj.rights:
        out["rights_objects"] = len(obj.rights)
        out["rights_sample"] = [
            {"object": r.object_fqn, "rights": {k: v for k, v in r.rights.items() if v}}
            for r in obj.rights[:30]
        ]
    if detail and obj.details:
        out["details"] = obj.details
    elif obj.details:
        out["details_available"] = len(obj.details)
    return out


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def get_object(kind: str, name: str, source: str = "", detail: bool = False,
               workspace: str = "") -> dict:
    """Структура объекта: синоним, реквизиты, ТЧ, перечисления, формы, модули, движения.

    detail=True добавляет полный сырой набор свойств (<Properties>) из метаданных."""
    ws = _ws(workspace)
    if err := _kind_ok(kind):
        return _err(err)
    if source:
        found, err2 = ws.find_objects(kind, name, source)
    else:
        found, err2 = ws.find_objects(kind, name)
    if err2:
        return _err(err2)
    parsed: list[tuple[str, MetaObject]] = []
    for cand_src, cand_ref in found:
        try:
            parsed.append((cand_src.name, ws.parse_object(cand_src, cand_ref)))
        except ValueError:
            continue
    if not parsed:
        return _err(f"Не удалось разобрать {kind}.{name}")
    # Заимствованный объект хранится в расширении ОГРЫЗКОМ: только изменённые части. Резолв
    # «расширения раньше базы» отдавал этот огрызок как всю структуру — на боевом документе это
    # 36 реквизитов из 49, НОЛЬ типов и ни одного признака обязательности, без всякой пометки.
    # Отдаём платформенное представление: объединение копий, значения расширения приоритетны.
    obj, merged_from = _merge_object_copies(parsed)
    payload = _object_payload(ws, obj, detail)
    payload["source"] = merged_from[0]
    if len(merged_from) > 1:
        payload["merged_from"] = merged_from  # как это видит платформа: база + расширения
    return payload


def _merge_object_copies(parsed: list[tuple[str, MetaObject]]) -> tuple[MetaObject, list[str]]:
    """Копии одного объекта (расширения + база) -> одна структура «как видит платформа».

    Основой берём самую полную копию (у заимствованного объекта это база), затем добавляем
    то, чего в ней нет: реквизиты, ТЧ, формы и модули из расширений. Порядок имён источников
    сохраняем как в резолве (расширения раньше базы) — он попадает в ответ."""
    names = [n for n, _ in parsed]
    base_name, base = max(parsed, key=lambda p: (len(p[1].fields), len(p[1].tabular)))
    if len(parsed) == 1:
        return base, names
    have_fields = {f.name.lower() for f in base.fields}
    have_tab = {t.name.lower() for t in base.tabular}
    have_forms = {f.name.lower() for f in base.forms}
    have_mods = {m.module_type.lower() for m in base.modules}
    for name, other in parsed:
        if name == base_name:
            continue
        base.fields += [f for f in other.fields if f.name.lower() not in have_fields]
        base.tabular += [t for t in other.tabular if t.name.lower() not in have_tab]
        base.forms += [f for f in other.forms if f.name.lower() not in have_forms]
        base.modules += [m for m in other.modules if m.module_type.lower() not in have_mods]
        # Движения и владельцев расширение тоже ДОПОЛНЯЕТ: без объединения ответ показывал
        # 1 регистр из 4 при том, что merged_from перечислял три источника.
        base.register_records += [r for r in other.register_records
                                  if r not in base.register_records]
        base.owners += [o for o in other.owners if o not in base.owners]
    return base, names


# --------------------------------------------------------------------------- #
# Код: чтение модулей и рутин
# --------------------------------------------------------------------------- #

def _resolve_module(ws: Workspace, kind: str, name: str, module: str, source: str,
                    routine: str = ""):
    """Resolve object+module extension-first; with `routine` prefer the source declaring it.

    Adopted objects exist in several sources — the extension's module holds only its own
    hooks, so a base routine must fall through to the base module instead of erroring."""
    if err := _kind_ok(kind):
        return None, None, None, _err(err)
    if kind == "Configuration":
        # Модули приложения и сеанса лежат прямо в папке Configuration и НЕ являются объектом
        # метаданных, поэтому обычный резолв по перечислению объектов их не находит. Ищем файл
        # напрямую и берём копию с непустым содержимым: у расширений это обычно заглушка.
        srcs, serr = ws.resolve_sources(source)
        if serr:
            return None, None, None, _err(serr)
        stem = module or name or "ManagedApplicationModule"
        best: tuple[int, object, Path] | None = None
        for s in srcs:
            cand = s.files_root / "Configuration" / f"{stem}.bsl"
            if cand.is_file():
                size = len(read_text(cand).splitlines())
                if best is None or size > best[0]:
                    best = (size, s, cand)
        if best is None:
            return None, None, None, _err(
                f"Модуль конфигурации '{stem}' не найден. Доступны: ManagedApplicationModule, "
                "OrdinaryApplicationModule, SessionModule, ExternalConnectionModule.")
        return ws, best[1], best[2], None
    cands, err2 = ws.find_objects(kind, name, source)
    if err2:
        return None, None, None, _err(err2)
    first_msg: str | None = None
    with_module = []
    for src, ref in cands:
        path, msg = ws.module_path(src, kind, ref[0].partition(".")[2], module)
        if path is None:
            first_msg = first_msg or msg
            continue
        if not routine or code_intel.find_in_module(path, routine):
            return ws, src, path, None
        with_module.append((src, path))
    if with_module:  # module exists, routine nowhere: let the caller report "есть: ..."
        src, path = with_module[0]
        return ws, src, path, None
    return None, None, None, _err(
        first_msg or f"Модуль '{module}' не найден у {kind}.{name} ни в одном источнике."
    )


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def list_routines(kind: str, name: str, module: str = "Module", source: str = "",
                  max_results: int = 100, offset: int = 0, exported_only: bool = False,
                  name_filter: str = "", workspace: str = "") -> dict:
    """Процедуры/функции модуля: сигнатуры, Экспорт, директивы, точки входа, override-аннотации.

    module: Module|Object|Manager|RecordSet|Value|Command|Form:<Имя>|<имя .bsl>.
    В крупных модулях 1С бывает больше тысячи рутин, поэтому ответ ограничен бюджетом:
    routine_count — всего в модуле, отдаётся окно max_results от offset; сузить можно
    exported_only=True или name_filter=<подстрока имени>."""
    ws, src, path, err = _resolve_module(_ws(workspace), kind, name, module, source)
    if err:
        return err
    routines = code_intel.routines_of(path)
    flt = name_filter.lower()
    selected = [rt for rt in routines
                if (not exported_only or rt.export) and (not flt or flt in rt.name.lower())]
    start = max(0, offset)
    window = selected[start: start + max(1, max_results)]
    rows = [code_intel.routine_row(path, rt) for rt in window]
    return {"source": src.name, "object": f"{kind}.{name}", "module": module,
            "path": ws.source_of_path(path)[1],
            "routine_count": len(routines), "matched": len(selected),
            "offset": start, "returned": len(rows),
            "truncated": start + len(rows) < len(selected), "routines": rows}


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def read_module(kind: str, name: str, module: str = "Module", start_line: int = 1,
                max_lines: int = 400, source: str = "", workspace: str = "") -> dict:
    """Текст модуля с пагинацией (start_line/max_lines)."""
    ws, src, path, err = _resolve_module(_ws(workspace), kind, name, module, source)
    if err:
        return err
    lines = read_text(path).splitlines()
    start = max(1, start_line)
    chunk = lines[start - 1 : start - 1 + max(1, max_lines)]
    return {
        "source": src.name,
        "path": ws.source_of_path(path)[1],
        "total_lines": len(lines),
        "start_line": start,
        "end_line": start + len(chunk) - 1,
        "text": "\n".join(chunk),
    }


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def read_routine(kind: str = "", name: str = "", routine_name: str = "", module: str = "Module",
                 source: str = "", workspace: str = "") -> dict:
    """Тело одной процедуры/функции по имени (для заимствованных объектов рутина ищется по
    источникам: расширения, затем база).

    Достаточно одного routine_name — объект и модуль находятся сами (по индексу символов;
    без индекса — поиском объявления). kind+name+module указывают, только если нужно снять
    неоднозначность одноимённых рутин; при неоднозначности ответ перечислит кандидатов."""
    if not routine_name:
        return _err("read_routine требует routine_name=<имя процедуры/функции>; "
                    "kind/name/module необязательны (объект находится сам).")
    ws0 = _ws(workspace)
    if not (kind and name):
        found, ferr = _locate_routine(ws0, routine_name, source)
        if ferr:
            return ferr
        kind, name, module = found
    ws, src, path, err = _resolve_module(ws0, kind, name, module, source,
                                         routine=routine_name)
    if err:
        return err
    rt = code_intel.find_in_module(path, routine_name)
    if rt is None:
        avail = ", ".join(r.name for r in code_intel.routines_of(path)[:25])
        return _err(f"Рутина '{routine_name}' не найдена. Есть: {avail}")
    return {
        "source": src.name,
        "path": ws.source_of_path(path)[1],
        **code_intel.routine_row(path, rt),
        "text": code_intel.routine_body(path, rt),
    }


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def read_file(rel_path: str, start_line: int = 1, max_lines: int = 400, source: str = "", workspace: str = "") -> dict:
    """Любой файл источника по пути относительно его корня (.mdo, .form, .xml, .bsl)."""
    ws = _ws(workspace)
    srcs, serr = ws.resolve_sources(source)
    if serr:
        return _err(serr)
    # Файл может существовать в нескольких источниках, причём у расширения это часто ПУСТАЯ
    # заглушка (напр. Configuration/SessionModule.bsl). Победа «первого попавшегося» давала
    # total_lines=0 и пустой text БЕЗ ошибки — агент делал вывод «кода нет». Поэтому среди
    # найденных копий выбираем непустую, а прочие перечисляем в also_in.
    hits: list[tuple[str, Path, list[str]]] = []
    for s in srcs:
        path, _msg = ws.safe_path(s, rel_path)
        if path is not None:
            hits.append((s.name, path, read_text(path).splitlines()))
    if not hits:
        return _err(f"Файл не найден ни в одном источнике: {rel_path}")
    src_name, _path, lines = max(hits, key=lambda h: len(h[2]))
    start = max(1, start_line)
    chunk = lines[start - 1 : start - 1 + max(1, max_lines)]
    out = {"source": src_name, "path": rel_path, "total_lines": len(lines),
           "start_line": start, "end_line": start + len(chunk) - 1,
           "text": "\n".join(chunk)}
    others = [n for n, _p, ls in hits if n != src_name]
    if others:
        out["also_in"] = others
        out["empty_copies"] = [n for n, _p, ls in hits if n != src_name and not ls]
    return out


# --------------------------------------------------------------------------- #
# Поиск
# --------------------------------------------------------------------------- #

@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def search_code(pattern: str = "", kinds: list[str] | None = None, name_filter: str = "",
                regex: bool = True, case_sensitive: bool = False, max_results: int = 100,
                source: str = "", query: str = "", workspace: str = "") -> dict:
    """Полнотекстовый поиск по BSL-модулям (ripgrep; без rg — Python-фолбэк).

    kinds — ограничить видами (['CommonModule','Document']); name_filter — подстрока
    имени объекта-владельца; source — один источник. Искомое можно передать как pattern
    (основное имя) или query — синоним на случай путаницы с search_metadata/fts_search."""
    pattern = pattern or query
    if not pattern:
        return _err("search_code требует pattern=<подстрока или regex по коду>, например "
                    "pattern=\"ПроверитьЗаполнение\\\\s*\\\\(\". Для поиска ОБЪЕКТОВ — "
                    "search_metadata(query=…), для ранжированного поиска — fts_search(query=…).")
    ws = _ws(workspace)
    kindset = set(kinds) if kinds else None
    if kindset and (bad := kindset - set(TYPE_FOLDERS.values())):
        return _err(f"Неизвестные виды: {', '.join(sorted(bad))}")
    return search.search_code(
        ws, pattern, kinds=kindset, name_filter=name_filter, regex=regex,
        case_sensitive=case_sensitive, max_results=max_results, source=source,
    )


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def fts_search(query: str, limit: int = 20, unit: str = "", source: str = "", workspace: str = "") -> dict:
    """Ранжированный поиск (SQLite FTS5, BM25) по рутинам и карточкам объектов:
    CamelCase-подслова, вес имени выше тела, кириллица матчится с усечением окончаний.

    unit: 'routine' | 'object' | пусто (всё). Индекс строится и дообновляется в фоне
    (прогрев при загрузке воркспейса; для http — prebuild всех воркспейсов на старте;
    вручную — кнопка в админке / serve-lite --build-fts). Пока индекс ещё не готов, тул НЕ
    отдаёт пусто/ошибку, а прозрачно возвращает результат search_code (rg) с пометкой
    degraded='fts_index_building'; свежесть/момент сборки — в built_at. Это лексический
    ранжированный поиск, не семантика: синонимию без общих слов ловит только большой сервер."""
    ws = _ws(workspace)
    res = fts.index_for(ws).search(query, limit=limit, unit=unit, source=source)
    if res.get("ready") is False and "error" not in res:
        # индекс ещё строится — деградируем на подстрочный rg, чтобы агент всегда получил ответ
        fb = search.search_code(ws, query, regex=False, max_results=limit, source=source)
        fb["degraded"] = "fts_index_building"
        fb["fts_note"] = res.get("note")
        return fb
    return res


def _locate_routine(ws: Workspace, routine_name: str, source: str = "") -> tuple:
    """(kind, name, module) объекта, где объявлена рутина — чтобы вызывающему не требовалось
    знать kind/name заранее (в пилоте это была частая причина отказов инструмента).

    Неоднозначность не угадываем: если объявлений несколько в разных объектах, возвращаем
    ошибку со списком кандидатов, чтобы агент уточнил одним следующим вызовом."""
    decl = code_intel.find_declarations(ws, routine_name, max_results=10, source=source)
    rows = decl.get("declarations", [])
    if not rows:
        return None, _err(
            f"Рутина '{routine_name}' не найдена ни в одном источнике. "
            "Проверьте имя (find_routine покажет объявления по подстроке имени).")
    uniq = {(r.get("object"), r.get("module")) for r in rows}
    if len(uniq) > 1:
        listed = "; ".join(f"{o} ▸ {m}" for o, m in sorted(uniq, key=lambda x: str(x)))
        return None, _err(
            f"Рутина '{routine_name}' объявлена в нескольких местах: {listed}. "
            "Уточните kind+name (+module).")
    obj, module = next(iter(uniq))
    kind, _, name = str(obj).partition(".")
    return (kind, name, module or "Module"), None


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def find_routine(routine_name: str, exported_only: bool = False, max_results: int = 50,
                 source: str = "", offset: int = 0, substring: bool = False,
                 workspace: str = "") -> dict:
    """Где ОБЪЯВЛЕНА процедура/функция с этим именем (по всем источникам, точный парс).

    declaration_count — сколько объявлений ВСЕГО (у типовых обработчиков вроде
    ПриСозданииНаСервере их тысячи), отдаётся окно max_results от offset; окно упорядочено по
    значимости (экспортные и общие модули выше), а не по алфавиту. engine показывает, отвечал
    индекс или живой скан."""
    return code_intel.find_declarations(
        _ws(workspace), routine_name, exported_only=exported_only, max_results=max_results,
        source=source, decl_offset=offset, substring=substring,
    )


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def search_metadata(query: str = "", kinds: list[str] | None = None, max_results: int = 100,
                    source: str = "", pattern: str = "", workspace: str = "") -> dict:
    """Поиск объектов по имени и по тексту метаданных (синонимы и пр.).

    Сначала совпадения по имени (список объектов), затем текстовые совпадения в
    файлах метаданных (.xml/.mdo) с привязкой к объекту. Текст можно передать как query
    (основное имя) или pattern — синоним на случай путаницы с search_code."""
    query = query or pattern
    if not query:
        return _err("search_metadata требует query=<часть имени или текста метаданных>, "
                    "например query=\"Претензия\". Для поиска по КОДУ — search_code(pattern=…).")
    ws = _ws(workspace)
    kindset = set(kinds) if kinds else None
    srcs, serr = ws.resolve_sources(source)
    if serr:
        return _err(serr)
    q = query.lower()
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for s in srcs:
        for fqn, _meta, _dir in ws.listing(s):
            kind, _, name = fqn.partition(".")
            if kindset and kind not in kindset:
                continue
            if q in name.lower():
                key = (s.name, fqn)
                if key not in seen:
                    seen.add(key)
                    rows.append({"source": s.name, "object": fqn, "matched": "name"})
                    if len(rows) >= max_results:
                        return {"query": query, "matches": rows, "truncated": True}
    # Text pass over metadata files (synonyms, comments). Meta files sit at fixed depths:
    # configurator <Kind>/<Name>.xml, EDT <Kind>/<Name>/<Name>.mdo (subsystems nest deeper).
    for s in srcs:
        glob = "*.mdo" if s.fmt == "edt" else "*.xml"
        _engine, it = search.stream(
            ws, query, sources=[s], kinds=kindset, glob=glob, regex=False,
            max_hits=max_results * 20,
        )
        for abs_path, _line, text in it:
            src_name, rel = ws.source_of_path(Path(abs_path))
            parts = rel.split("/")
            if s.fmt == "edt":
                ok = len(parts) >= 3 and parts[-1] == f"{parts[-2]}.mdo"
            else:
                ok = (len(parts) == 2 or parts[0] == "Subsystems") and parts[-1].endswith(".xml")
            if not ok:
                continue
            kind = TYPE_FOLDERS.get(parts[0], parts[0])
            name = Path(parts[-1]).stem
            key = (src_name, f"{kind}.{name}")
            if key in seen:
                continue
            seen.add(key)
            rows.append({"source": src_name, "object": f"{kind}.{name}", "matched": "text",
                         "text": text.strip()[:200]})
            if len(rows) >= max_results:
                return {"query": query, "matches": rows, "truncated": True}
    return {"query": query, "matches": rows, "truncated": False}


# --------------------------------------------------------------------------- #
# Анализ кода
# --------------------------------------------------------------------------- #

@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def find_callees(kind: str, name: str, routine_name: str, module: str = "Module",
                 source: str = "", workspace: str = "") -> dict:
    """Кого вызывает рутина: разрешённые вызовы (local/common_module/manager) + неразрешённые."""
    return code_intel.find_callees(_ws(workspace), kind, name, module, routine_name, source)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def find_callers(routine_name: str = "", object_hint: str = "", kinds: list[str] | None = None,
                 max_results: int = 20, source: str = "", summary_only: bool = False,
                 name: str = "", workspace: str = "") -> dict:
    """Места ВЫЗОВА рутины (проверено парсером: объявления и строки/комментарии исключены).

    object_hint — имя общего модуля/объекта для отсечения одноимённых методов.
    Всегда отдаётся сводка: call_rows_total (сколько записей вызова всего), distinct_callers
    (сколько различных рутин вызывают) и by_object (топ объектов + by_object_total).
    Строки вызовов — до max_results, у каждой есть call_line; summary_only=True отдаёт только
    сводку. engine показывает, отвечал индекс или живой скан (scan — без полного счёта).
    Имя рутины принимается как routine_name (основное) или name — синоним."""
    routine_name = routine_name or name
    if not routine_name:
        return _err("find_callers требует routine_name=<имя процедуры/функции>.")
    return code_intel.find_callers(
        _ws(workspace), routine_name, object_hint=object_hint, kinds=kinds,
        max_results=max_results, source=source, summary_only=summary_only,
    )


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def call_graph(routine_name: str, depth: int = 2, max_per_level: int = 40,
               source: str = "", workspace: str = "") -> dict:
    """Восходящий граф вызовов: кто (рекурсивно) вызывает рутину; уровни с охватывающими рутинами."""
    return code_intel.call_graph(
        _ws(workspace), routine_name, depth=depth, max_per_level=max_per_level, source=source,
    )


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def find_overrides(kind: str = "", name: str = "", method: str = "", source: str = "",
                   max_results: int = 100, offset: int = 0, workspace: str = "") -> dict:
    """Переопределения расширений (&Вместо/&Перед/&После/&ИзменениеИКонтроль) с целями.

    Фильтры: kind+name — заимствованный объект; method — базовый метод; source — расширение.
    override_count — всего найдено (полный детерминированный счёт), отдаётся окно
    offset..offset+max_results; truncated=true означает «есть ещё», доборка — увеличить offset."""
    return code_intel.find_overrides(_ws(workspace), kind=kind, name=name, method=method,
                                     source=source, max_results=max_results, offset=offset)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def find_handlers(kind: str, name: str, source: str = "", workspace: str = "") -> dict:
    """Обработчики объекта: события форм (+объявлен ли обработчик) и точки входа модулей
    (проведение/запись/проверка_заполнения/...)."""
    if err := _kind_ok(kind):
        return _err(err)
    return code_intel.find_handlers(_ws(workspace), kind, name, source)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def writes_to(document: str = "", register: str = "", source: str = "", workspace: str = "") -> dict:
    """Движения: document='Заказ' -> его регистры; register='ОстаткиТоваров' -> кто в него пишет."""
    return code_intel.writes_to(_ws(workspace), document=document, register=register,
                                source=source)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def metrics(source: str = "", workspace: str = "") -> dict:
    """Инвентарь рабочей копии: объекты по видам, файлы/байты кода, число рутин, overrides."""
    return code_intel.metrics(_ws(workspace), source=source)


# --------------------------------------------------------------------------- #
# Зависимости (метаданные)
# --------------------------------------------------------------------------- #

@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def get_dependencies(kind: str, name: str, source: str = "", workspace: str = "") -> dict:
    """Связи объекта: исходящие (ссылочные реквизиты по всем источникам, владельцы,
    движения) и входящие (кто ссылается на тип, подписки на события; для регистров —
    какие документы пишут). Метаданные-уровень; использование в коде — search_code."""
    if err := _kind_ok(kind):
        return _err(err)
    return code_intel.get_dependencies(_ws(workspace), kind, name, source)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def find_type_usages(kind: str, name: str, max_results: int = 100, source: str = "",
                     offset: int = 0, workspace: str = "") -> dict:
    """Где используется ТИП объекта в метаданных: реквизиты объектов и форм, подписки,
    определяемые типы — точные строки файлов (`<Вид>Ref.<Имя>`/`<Вид>Object.<Имя>`).

    usage_count — сколько использований ВСЕГО (у ходовых типов это больше тысячи), отдаётся
    окно max_results от offset; by_object — распределение по объектам-владельцам."""
    if err := _kind_ok(kind):
        return _err(err)
    return code_intel.type_usages(_ws(workspace), kind, name, max_results=max_results,
                                  source=source, offset=offset)


# --------------------------------------------------------------------------- #
# Git-осведомлённость: изменения рабочей копии
# --------------------------------------------------------------------------- #

@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def changed_objects(ref: str = "", source: str = "", include_untracked: bool = True,
                    workspace: str = "") -> dict:
    """Что изменено в рабочей копии: git status (ref пуст) или diff против ref
    (ветка/коммит/'HEAD~1'), сгруппировано по объектам метаданных.

    У каждого изменения — файл, git-статус и вид артефакта (module/meta/form_layout).
    include_untracked=False убирает обход неотслеживаемых файлов (он дороже самого диффа) —
    быстрее, но новые, ещё не добавленные в git модули в ответ не попадут."""
    return gitview.changed_objects(_ws(workspace), ref, source,
                                   include_untracked=include_untracked)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def review_set(ref: str = "", max_callers: int = 5, source: str = "", detail: bool = False,
               max_routines: int = 25, offset: int = 0, include_untracked: bool = True,
               workspace: str = "") -> dict:
    """Ревью-набор изменений: изменённые строки → затронутые рутины → их вызывающие,
    точки входа и override-хуки расширений поверх них.

    Отвечает на «что я сломал этой правкой»: каждый вызывающий проверен парсером,
    untracked-модули включаются целиком. ref как в changed_objects. Вызывающие по умолчанию —
    компактные строки `Объект▸Модуль▸Рутина:строка`; detail=True даёт полные записи.

    routine_count — сколько рутин затронуто ВСЕГО; отдаётся окно max_routines, отранжированное
    по риску (экспортность, точка входа, переопределения, число вызывающих), доборка — offset."""
    return gitview.review_set(_ws(workspace), ref, max_callers=max_callers, source=source,
                              detail=detail, max_routines=max_routines, offset=offset,
                              include_untracked=include_untracked)


# --------------------------------------------------------------------------- #
# UI и интеграции: формы и сервисы
# --------------------------------------------------------------------------- #

def _mark_declared(rows: list[dict], declared: dict, key: str = "handler") -> None:
    for r in rows:
        h = (r.get(key) or "").strip()
        if not h:
            continue
        rt = declared.get(h.lower())
        r["declared"] = rt is not None
        if rt is not None:
            r["lines"] = [rt.start_line, rt.end_line]


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def get_service(name: str, source: str = "", workspace: str = "") -> dict:
    """Интроспекция сервиса: HTTPService (rootURL, шаблоны URL, методы) или WebService
    (namespace, операции с параметрами). Обработчики сверяются с модулем сервиса
    (declared/lines) — сразу видно, какие методы не реализованы."""
    ws = _ws(workspace)
    for kind, parser in (
        ("HTTPService", metaview.parse_http_service),
        ("WebService", metaview.parse_web_service),
    ):
        cands, err = ws.find_objects(kind, name, source)
        if err:
            continue
        src, ref = cands[0]
        data = parser(ref[1])
        if data is None:
            return _err(f"Не удалось разобрать {kind}.{name}: {ref[1]}")
        mpath, _msg = ws.module_path(src, kind, ref[0].partition(".")[2], "Module")
        declared: dict = {}
        if mpath is not None:
            declared = {r.name.lower(): r for r in code_intel.routines_of(mpath)}
        if data["kind"] == "HTTPService":
            for t in data["url_templates"]:
                _mark_declared(t["methods"], declared)
        else:
            _mark_declared(data["operations"], declared)
        data["source"] = src.name
        data["module_path"] = ws.source_of_path(mpath)[1] if mpath is not None else None
        if len(cands) > 1:
            data["also_in"] = [s.name for s, _ in cands[1:]]
        return data
    scope = f" в источнике '{source}'" if source else ""
    return _err(f"Сервис '{name}' не найден{scope} (искал HTTPService и WebService).")


def _form_files(ws: Workspace, src, obj_dir: Path, kind: str, name: str,
                form: str) -> tuple[Path | None, Path | None]:
    """(form layout xml, form module) for one candidate source; CommonForm — сам объект."""
    if kind == "CommonForm":
        fxml = obj_dir / ("Form.form" if src.fmt == "edt" else Path("Ext") / "Form.xml")
        return (fxml if fxml.is_file() else None,
                ws.form_module_path(src, obj_dir, name, common_form_dir=obj_dir))
    return ws.form_xml_path(src, obj_dir, form), ws.form_module_path(src, obj_dir, form)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def get_form(kind: str, name: str, form: str = "", source: str = "", items_limit: int = 60,
             summary: bool = False, workspace: str = "") -> dict:
    """Структура формы: реквизиты, команды (+обработчики), элементы (поля с dataPath,
    кнопки с командами, группы), обработчики событий формы/элементов с пометкой declared.

    Для CommonForm параметр form не нужен. Форма расширения затеняет одноимённую базовую;
    секции, которых в форме расширения нет (реквизиты/команды), дополняются из базовой
    формы с пометкой attributes_source/commands_source — как их видит платформа.

    counts всегда показывает полные размеры; дерево элементов ограничено items_limit и
    упорядочено по «сигнальности» (обработчики, команды, dataPath — выше). items_limit=0 —
    отдать все элементы; summary=True — только элементы, несущие логику."""
    ws = _ws(workspace)
    if err := _kind_ok(kind):
        return _err(err)
    cands, err = ws.find_objects(kind, name, source)
    if err:
        return _err(err)
    if kind != "CommonForm" and not form:
        return _err(f"Укажите имя формы. {_forms_hint(ws, cands)}")
    for i, (src, ref) in enumerate(cands):
        fxml, module = _form_files(ws, src, ref[2], kind, name, form)
        if fxml is None:
            continue  # у расширения этой формы нет — падаем к базовому источнику
        data = metaview.parse_form(Path(fxml))
        if "error" in data:
            return _err(data["error"])
        declared: dict = {}
        if module is not None:
            declared = {r.name.lower(): r for r in code_intel.routines_of(module)}
        _mark_declared(data["form_handlers"], declared)
        _mark_declared(data["commands"], declared)
        for item in data["items"]:
            _mark_declared(item.get("handlers") or [], declared)
        # Форма расширения хранит только дерево элементов: пустые реквизиты/команды —
        # это унаследованные от базовой формы, достроим их оттуда.
        if src.is_extension and (not data["attributes"] or not data["commands"]):
            for bsrc, bref in cands[i + 1:]:
                bxml, bmodule = _form_files(ws, bsrc, bref[2], kind, name, form)
                if bxml is None:
                    continue
                base = metaview.parse_form(Path(bxml))
                if "error" in base:
                    break
                bdeclared: dict = {}
                if bmodule is not None:
                    bdeclared = {r.name.lower(): r for r in code_intel.routines_of(bmodule)}
                if not data["attributes"] and base["attributes"]:
                    data["attributes"] = base["attributes"]
                    data["attributes_source"] = bsrc.name
                if not data["commands"] and base["commands"]:
                    _mark_declared(base["commands"], bdeclared)
                    data["commands"] = base["commands"]
                    data["commands_source"] = bsrc.name
                break
        data.update({
            "source": src.name,
            "object": f"{kind}.{name}",
            "form": form or name,
            "has_module": module is not None,
            "counts": {
                "attributes": len(data["attributes"]),
                "commands": len(data["commands"]),
                "items": len(data["items"]),
            },
        })
        # Бюджет ответа: у ходовых форм 1С сотни элементов (у ФормаДокумента РТУ — 245), и
        # полная выдача — это ~18 тыс. токенов в одном вызове. Счётчики выше сохраняются, а
        # дерево элементов сужается: сперва то, что несёт логику (обработчики, dataPath,
        # команды), затем остальное. Полный список — items_limit=0.
        items = data.get("items") or []
        if items_limit and len(items) > items_limit:
            def _signal(it: dict) -> tuple:
                return (bool(it.get("handlers")), bool(it.get("command")),
                        bool(it.get("data_path")))
            ranked = sorted(items, key=_signal, reverse=True)
            data["items"] = ranked[:items_limit]
            data["items_returned"] = len(data["items"])
            data["items_ranked_by_signal"] = True
        if summary:
            # Только каркас: счётчики, реквизиты, команды и элементы с логикой.
            data["items"] = [it for it in data["items"]
                             if it.get("handlers") or it.get("command")]
            data["items_returned"] = len(data["items"])
            data["summary"] = True
        return data
    return _err(f"Форма '{form or name}' не найдена у {kind}.{name}. {_forms_hint(ws, cands)}")


def _forms_hint(ws: Workspace, cands: list) -> str:
    names: list[str] = []
    for src, ref in cands:
        try:
            obj = ws.parse_object(src, ref)
        except ValueError:
            continue
        names.extend(f.name for f in obj.forms)
    uniq = list(dict.fromkeys(names))
    return f"Доступные формы: {', '.join(uniq)}." if uniq else "У объекта нет форм."


# --------------------------------------------------------------------------- #
# Справка платформы (синтаксис-помощник, .hbk) — те же имена инструментов, что у
# большого сервера; вместо векторов — индекс имён, текст страницы читается из .hbk.
# --------------------------------------------------------------------------- #

@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def platform_versions() -> dict:
    """Настроенные сборки справки платформы: версии, файлы .hbk, число тем.

    topics = null, пока индекс не построен (строится при первом запросе или кнопкой в админке)."""
    return _help().versions()


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def platform_docinfo(name: str, platform_version: str = "") -> dict:
    """Синтаксис-помощник: точный лукап темы по каноническому имени — русскому
    («Массив.Найти»), английскому («Array.Find») или короткому («Найти», с дизамбигуацией).

    platform_version сужает до конкретной сборки. Первый вызов строит индекс имён
    (десятки секунд на версию), дальше — мгновенно."""
    return _help().docinfo(name, platform_version)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def platform_get_document(name: str, platform_version: str = "") -> dict:
    """Полный текст темы справки по точному имени («Объект.Метод») или fqn
    `platform_help:<версия>|<Имя>`. Без версии берётся самая свежая сборка."""
    return _help().get_document(name, platform_version)


@mcp.tool(structured_output=False)  # без дубля в structuredContent: он удваивал ответ
def platform_search(query: str, platform_version: str = "", limit: int = 20) -> dict:
    """Поиск по НАЗВАНИЯМ тем справки (подстрока, RU/EN) — навигация по API платформы.

    Семантический поиск по содержимому справки — у большого onec-vecgraph сервера."""
    return _help().search_titles(query, platform_version, limit)


# --------------------------------------------------------------------------- #
# Веб-админка (opt-in: ONEC_LITE_ADMIN=true / serve-lite --admin; только http-транспорт)
# --------------------------------------------------------------------------- #

def _admin_enabled() -> bool:
    return os.environ.get("ONEC_LITE_ADMIN", "").strip().lower() in ("1", "true", "yes", "on")


def _snapshot(workspace: str = "") -> dict:
    """Admin view: selected workspace details + каталог всех воркспейсов + help/rg/fts."""
    _init_rg_from_state()
    name = (workspace or "").strip() or default_workspace_name()
    ws: Workspace | None = None
    try:
        ws = _ws(name)
    except RuntimeError:
        pass  # честно «не настроен» — пути задаются формой админки
    snap = lite_admin.workspace_snapshot(ws)
    snap["workspace"] = name
    wss, active = lite_admin.load_workspaces(lite_admin.state_file())
    for wname, loaded in _WORKSPACES.items():  # env/--root конфигурации вне state
        wss.setdefault(wname, {"root": str(loaded.root),
                               "ext_roots": [str(p) for p in loaded.ext_roots]})
    rows = []
    for n, e in sorted(wss.items()):
        row_root = _entry_root(n, e) if (e.get("repo") or e.get("root")) else Path(e.get("root") or "")
        rows.append({
            "name": n, "root": str(row_root), "ext_roots": e.get("ext_roots") or [],
            "repo": e.get("repo") or "", "branch": e.get("branch") or "",
            "update_on_start": e.get("update_on_start") or "off",
            "kind": "mirror" if e.get("repo") else "path",
            "cloned": (row_root / ".git").is_dir() if e.get("repo") else None,
            "active": n == active, "loaded": n in _WORKSPACES, "selected": n == name,
            "git": gitops.status_brief(row_root) if row_root.is_dir() else {"git": False},
            "last_update": _UPDATE_RESULTS.get(n),
        })
    snap["workspaces"] = rows
    snap["active"] = active
    snap["default_workspace"] = default_workspace_name()
    snap["rg"] = search.rg_path()
    snap["rg_override"] = search.rg_override()
    snap["state_file"] = str(lite_admin.state_file())
    help_cat = _help()
    hv = help_cat.versions()
    snap["platform_help"] = {
        "entries": help_cat.entries,
        "versions": hv["versions"],
        "indexed": hv["indexed"],
    }
    snap["fts"] = (
        fts.index_for(ws).status() if ws is not None
        else {"available": fts.fts_available(), "built": False}
    )
    return snap


def apply_admin_paths(
    root: str, ext_text: str, help_text: str = "", rg_text: str | None = None,
    name: str = "", repo: str = "", branch: str = "", update_on_start: str = "",
) -> tuple[dict | None, str | None]:
    """Upsert workspace `name` (путь ИЛИ git-зеркало) + help/rg and persist; (snapshot, errors).

    repo задаёт управляемое зеркало: клонируется/обновляется сразу (синхронно, как
    построение индексов). Частичный успех допустим; в state сохраняется ФАКТИЧЕСКИ
    применённое, а не введённое — битый путь не переживёт рестарт."""
    _init_rg_from_state()
    root = (root or "").strip().strip('"').strip()
    repo = (repo or "").strip().strip('"').strip()
    branch = (branch or "").strip()
    mode = (update_on_start or "").strip().lower()
    if mode not in lite_admin.UPDATE_MODES:
        mode = "off"
    ext = lite_admin.parse_ext_roots(ext_text)
    help_entries = platform_help.parse_help_lines(help_text)
    errors: list[str] = []
    ws_name = (name or "").strip() or default_workspace_name()
    if lite_admin.normalize_ws_name(ws_name) is None:
        return None, f"Недопустимое имя воркспейса: '{ws_name}' (буквы/цифры/_/-/., до 64)."
    if rg_text is not None:
        cleaned = rg_text.strip().strip('"')
        if cleaned and not Path(cleaned).is_file():
            errors.append(f"ripgrep: файл не найден: {cleaned}")
        else:
            search.set_rg_path(cleaned or None)  # пусто = вернуться к автопоиску
    if not root and not repo and not help_entries and rg_text is None:
        return None, "Укажите корень конфигурации, git-URL зеркала и/или пути к справке."
    if repo:
        res = gitops.update_workspace(ws_name, {"repo": repo, "branch": branch})
        _UPDATE_RESULTS[ws_name] = res
        if not res.get("ok"):
            errors.append(f"Зеркало: {res.get('error')}")
        else:
            ws = None
            try:
                ws = configure(gitops.mirror_path(ws_name), tuple(ext), name=ws_name)
            except Exception as exc:  # noqa: BLE001 - клон есть, но не парсится как конфигурация
                errors.append(f"Рабочая копия: {exc}")
            if ws is not None:
                try:
                    lite_admin.upsert_workspace(
                        lite_admin.state_file(), ws_name, "", [str(p) for p in ws.ext_roots],
                        repo=repo, branch=branch, update_on_start=mode,
                    )
                except OSError as exc:
                    errors.append(f"Состояние не сохранено: {exc}")
    elif root:
        ws = None
        try:
            ws = configure(root, tuple(ext), name=ws_name)
        except Exception as exc:  # noqa: BLE001 - показать причину, оставив прежний workspace
            errors.append(f"Рабочая копия: {exc}")
        if ws is not None:
            try:
                lite_admin.upsert_workspace(
                    lite_admin.state_file(), ws_name, str(ws.root),
                    [str(p) for p in ws.ext_roots], update_on_start=mode,
                )
            except OSError as exc:
                errors.append(f"Состояние не сохранено: {exc}")
    errors.extend(f"Справка: {e}" for e in configure_help(help_entries))
    try:
        wss, active = lite_admin.load_workspaces(lite_admin.state_file())
        lite_admin.save_state(
            lite_admin.state_file(), wss, active,
            platform_help=_HELP.entries, rg_path=search.rg_override() or "",
        )
    except OSError as exc:
        errors.append(f"Состояние не сохранено: {exc}")
    return _snapshot(ws_name), ("; ".join(errors) if errors else None)


@mcp.custom_route("/admin", methods=["GET", "POST"])
async def admin_page(request: Request) -> Response:
    """Статус воркспейса + форма путей (база/расширения). Применение на лету, без рестарта.

    Неаутентифицировано — держать на loopback (дефолт) или за прокси с авторизацией."""
    if not _admin_enabled():
        return PlainTextResponse(
            "admin is disabled (set ONEC_LITE_ADMIN=true or run serve-lite --admin)",
            status_code=404,
        )
    if request.method == "POST":
        form = await request.form()
        action = form.get("action") or "apply"
        sel = str(form.get("ws") or "").strip()

        def _redir(param: str, text: str, ws_name: str = "") -> RedirectResponse:
            target = ws_name or sel
            prefix = f"admin?ws={quote(target)}&" if target else "admin?"
            return RedirectResponse(prefix + param + "=" + quote(text), status_code=303)

        if action == "refresh":
            for loaded in _WORKSPACES.values():
                loaded.refresh()
            code_intel.clear_caches()
            _help().refresh()
            return _redir("msg", "Кэши сброшены")
        if action == "activate":
            if lite_admin.set_active(lite_admin.state_file(), sel):
                return _redir("msg", f"Активный воркспейс: {sel}")
            return _redir("err", f"Воркспейс '{sel}' не найден в сохранённом состоянии.")
        if action == "delete":
            _WORKSPACES.pop(sel, None)
            if lite_admin.delete_workspace(lite_admin.state_file(), sel):
                return RedirectResponse(
                    "admin?msg=" + quote(f"Воркспейс '{sel}' удалён (индексы на диске не тронуты)."),
                    status_code=303)
            return _redir("err", f"Воркспейс '{sel}' не найден в сохранённом состоянии.")
        if action == "update_ws":
            wss, _active = lite_admin.load_workspaces(lite_admin.state_file())
            entry = wss.get(sel)
            if entry is None:
                return _redir("err", f"Воркспейс '{sel}' не найден в сохранённом состоянии.")
            res = gitops.update_workspace(sel, entry, mode=str(form.get("mode") or ""))
            res["trigger"] = "admin"
            _UPDATE_RESULTS[sel] = res
            if not res.get("ok"):
                return _redir("err", f"{res.get('op')}: {res.get('error')}")
            loaded = _WORKSPACES.get(sel)
            if loaded is not None:
                loaded.refresh()
                code_intel.clear_caches()
            brief = res.get("output") or "готово"
            extra = f" · ветка {res.get('branch')}" if res.get("branch") else ""
            return _redir("msg", f"{res.get('op')}: {brief[:160]}{extra}")
        if action == "build_fts":
            try:
                ws = _ws(sel)
            except RuntimeError as exc:
                return _redir("err", str(exc))
            res = fts.index_for(ws).build()
            if "error" in res:
                return _redir("err", res["error"])
            msg = (f"Индекс поиска: +{res['files_added']} файлов, ~{res['files_updated']} "
                   f"обновлено, -{res['files_removed']}; юнитов записано "
                   f"{res['units_written']} за {res['seconds']} с (всего {res.get('units')})")
            return _redir("msg", msg)
        if action == "build_help":
            cat = _help()
            if not cat.entries:
                return _redir("err", "Сначала задайте и примените пути к справке.")
            from time import perf_counter

            t0 = perf_counter()
            topics = len(cat.index())
            return _redir("msg", f"Индекс справки построен: {topics} тем за {perf_counter() - t0:.1f} с")
        name = str(form.get("ws_name") or "").strip() or sel
        _snap, err = apply_admin_paths(
            str(form.get("root") or ""),
            str(form.get("ext_roots") or ""),
            str(form.get("help_paths") or ""),
            rg_text=str(form.get("rg_path") or ""),
            name=name,
            repo=str(form.get("repo") or ""),
            branch=str(form.get("branch") or ""),
            update_on_start=str(form.get("update_on_start") or ""),
        )
        if err:
            return _redir("err", err, ws_name=name)
        return _redir("msg", "Пути применены и сохранены", ws_name=name)
    return HTMLResponse(lite_admin.render_admin_page(
        _snapshot(request.query_params.get("ws", "")),
        rg=search.rg_path(),
        state_path=str(lite_admin.state_file()),
        message=request.query_params.get("msg", ""),
        error=request.query_params.get("err", ""),
    ))


@mcp.custom_route("/admin.json", methods=["GET"])
async def admin_json(request: Request) -> Response:
    """Машиночитаемое состояние воркспейсов (?ws=<имя> — выбрать; то же, что на странице)."""
    if not _admin_enabled():
        return JSONResponse({"error": "admin disabled"}, status_code=404)
    return JSONResponse(_snapshot(request.query_params.get("ws", "")))


def _prebuild_all_workspaces() -> None:
    """HTTP shared-сервис: фоново прогреть FTS-индексы всех сконфигурированных воркспейсов,
    чтобы первый запрос любого tenant'а не упирался в «индекс ещё строится». Один фоновый
    поток последовательно грузит воркспейсы; сборка каждого идёт в своём потоке (по одному
    писателю на воркспейс). Отключить: ONEC_LITE_FTS_AUTOBUILD=off."""
    if not _fts_autobuild_enabled():
        return

    def _run() -> None:
        wss, _active = lite_admin.load_workspaces(lite_admin.state_file())
        names = list(wss)
        if not names and (os.environ.get("ONEC_LITE_ROOT") or _WORKSPACES):
            names = [default_workspace_name()]
        for name in names:
            try:
                _ws(name)  # загрузка + update_on_start + _maybe_build_fts (прогрев индекса)
            except Exception:  # noqa: BLE001 — один плохой воркспейс не срывает прогрев прочих
                logging.getLogger(__name__).exception("fts prebuild: воркспейс %s", name)

    threading.Thread(target=_run, name="fts-prebuild", daemon=True).start()


def run(transport: str = "stdio") -> None:
    if transport == "stdio":
        mcp.run("stdio")
    else:
        _prebuild_all_workspaces()
        mcp.run("streamable-http")
