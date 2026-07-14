"""onec-lite — zero-infrastructure MCP server over a live 1C working copy.

Reads a Configurator XML dump or a 1C:EDT workspace (base + extensions) directly from
disk: no Neo4j, no embeddings, results always match the current files. Search is
ripgrep-accelerated; code answers are verified by the project's BSL parser.

Start via CLI: `onec-vecgraph serve-lite --root <путь>` (stdio by default).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from ..chunking import KIND_RU
from ..parsing.dump import TYPE_FOLDERS
from ..parsing.model import MetaObject
from . import admin as lite_admin
from . import code_intel, fts, gitview, metaview, platform_help, search
from .workspace import Workspace, read_text

INSTRUCTIONS = """onec-lite: навигация по ЖИВОЙ рабочей копии конфигурации 1С (база + расширения),
без векторизации — данные всегда соответствуют текущим файлам на диске.

Словарь: kind — вид метаданных (Catalog, Document, CommonModule, ...); module — псевдоним
модуля (Module|Object|Manager|RecordSet|Value|Command|Form:<Имя>|<имя файла .bsl>);
source — имя источника из overview() (пусто = все, расширения раньше базы).

Куда идти: обзор -> overview/metrics; структура -> list_objects/get_object;
зависимости -> get_dependencies (связи объекта) / find_type_usages (где используется тип);
код -> list_routines/read_module/read_routine; поиск -> fts_search (ранжированный BM25,
лучший старт для «где считается X»; требует построенного индекса) /
search_code (точная подстрока/regex) / search_metadata / find_routine;
анализ -> find_callers/find_callees/call_graph/find_handlers/find_overrides/writes_to;
изменения ветки (git) -> changed_objects (что поменялось) / review_set (затронутые
рутины + их вызывающие — ревью-набор незакоммиченной работы);
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

_WS: Workspace | None = None
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


def configure(root: str | Path, ext_roots: tuple[str | Path, ...] = ()) -> Workspace:
    """Build and swap in the module-level workspace (CLI startup / admin apply).

    The swap happens only after Workspace() succeeds, so a bad path keeps serving the
    previous workspace. Code-intel caches are dropped: source names may stay the same
    while pointing at a different checkout."""
    global _WS
    _init_rg_from_state()
    ws = Workspace(root, ext_roots)
    _WS = ws
    code_intel.clear_caches()
    return ws


def _ws() -> Workspace:
    if _WS is None:
        root = os.environ.get("ONEC_LITE_ROOT", "").strip()
        ext: tuple[str, ...] = tuple(
            lite_admin.parse_ext_roots(os.environ.get("ONEC_LITE_EXT_ROOTS", ""))
        )
        if not root:
            saved = lite_admin.load_paths(lite_admin.state_file())
            if saved:
                root, saved_ext = saved
                ext = ext or tuple(saved_ext)
        if not root:
            raise RuntimeError(
                "Workspace не сконфигурирован: задайте --root/ONEC_LITE_ROOT "
                "или откройте админку (/admin при serve-lite --admin --transport http)."
            )
        configure(root, ext)
    assert _WS is not None
    return _WS


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


def _kind_ok(kind: str) -> str | None:
    if kind in set(TYPE_FOLDERS.values()):
        return None
    near = ", ".join(sorted(k for k in TYPE_FOLDERS.values() if kind.lower() in k.lower())[:5])
    return f"Неизвестный вид метаданных '{kind}'." + (f" Похожие: {near}." if near else "")


# --------------------------------------------------------------------------- #
# Обзор / структура
# --------------------------------------------------------------------------- #

@mcp.tool()
def overview() -> dict:
    """Обзор рабочей копии: источники (база + расширения) и число объектов по видам."""
    ws = _ws()
    return {
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
    }


@mcp.tool()
def list_kinds() -> dict:
    """Все допустимые значения параметра kind (+ русские названия)."""
    kinds = sorted(set(TYPE_FOLDERS.values()))
    return {"kinds": kinds, "ru": {k: KIND_RU[k] for k in kinds if k in KIND_RU}}


@mcp.tool()
def list_objects(kind: str, filter: str = "", limit: int = 200, source: str = "") -> dict:
    """Объекты вида по всем источникам; filter — подстрока имени (без регистра).

    Совпадение имени в нескольких источниках отражается полем in_multiple_sources."""
    ws = _ws()
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
            {"name": f.name, "synonym": f.synonym, "role": f.role, "type": f.type_text}
            for f in obj.fields[:100]
        ]
        if len(obj.fields) > 100:
            out["attributes_total"] = len(obj.fields)
    if obj.tabular:
        out["tabular_sections"] = [
            {
                "name": t.name,
                "synonym": t.synonym,
                "attributes": [
                    {"name": f.name, "synonym": f.synonym, "type": f.type_text}
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


@mcp.tool()
def get_object(kind: str, name: str, source: str = "", detail: bool = False) -> dict:
    """Структура объекта: синоним, реквизиты, ТЧ, перечисления, формы, модули, движения.

    detail=True добавляет полный сырой набор свойств (<Properties>) из метаданных."""
    ws = _ws()
    if err := _kind_ok(kind):
        return _err(err)
    src, ref, also, err2 = ws.find_object(kind, name, source)
    if err2:
        return _err(err2)
    assert src is not None and ref is not None
    try:
        obj = ws.parse_object(src, ref)
    except ValueError as exc:
        return _err(str(exc))
    payload = _object_payload(ws, obj, detail)
    payload["source"] = src.name
    if also:
        payload["also_in"] = also
    return payload


# --------------------------------------------------------------------------- #
# Код: чтение модулей и рутин
# --------------------------------------------------------------------------- #

def _resolve_module(kind: str, name: str, module: str, source: str, routine: str = ""):
    """Resolve object+module extension-first; with `routine` prefer the source declaring it.

    Adopted objects exist in several sources — the extension's module holds only its own
    hooks, so a base routine must fall through to the base module instead of erroring."""
    ws = _ws()
    if err := _kind_ok(kind):
        return None, None, None, _err(err)
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


@mcp.tool()
def list_routines(kind: str, name: str, module: str = "Module", source: str = "") -> dict:
    """Процедуры/функции модуля: сигнатуры, Экспорт, директивы, точки входа, override-аннотации.

    module: Module|Object|Manager|RecordSet|Value|Command|Form:<Имя>|<имя .bsl>."""
    ws, src, path, err = _resolve_module(kind, name, module, source)
    if err:
        return err
    rows = [code_intel.routine_row(path, rt) for rt in code_intel.routines_of(path)]
    return {"source": src.name, "object": f"{kind}.{name}", "module": module,
            "path": ws.source_of_path(path)[1], "routine_count": len(rows), "routines": rows}


@mcp.tool()
def read_module(kind: str, name: str, module: str = "Module", start_line: int = 1,
                max_lines: int = 400, source: str = "") -> dict:
    """Текст модуля с пагинацией (start_line/max_lines)."""
    ws, src, path, err = _resolve_module(kind, name, module, source)
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


@mcp.tool()
def read_routine(kind: str, name: str, routine_name: str, module: str = "Module",
                 source: str = "") -> dict:
    """Тело одной процедуры/функции модуля по имени (для заимствованных объектов рутина
    ищется по источникам: расширения, затем база)."""
    ws, src, path, err = _resolve_module(kind, name, module, source, routine=routine_name)
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


@mcp.tool()
def read_file(rel_path: str, start_line: int = 1, max_lines: int = 400, source: str = "") -> dict:
    """Любой файл источника по пути относительно его корня (.mdo, .form, .xml, .bsl)."""
    ws = _ws()
    srcs, serr = ws.resolve_sources(source)
    if serr:
        return _err(serr)
    for s in srcs:
        path, _msg = ws.safe_path(s, rel_path)
        if path is not None:
            lines = read_text(path).splitlines()
            start = max(1, start_line)
            chunk = lines[start - 1 : start - 1 + max(1, max_lines)]
            return {"source": s.name, "path": rel_path, "total_lines": len(lines),
                    "start_line": start, "end_line": start + len(chunk) - 1,
                    "text": "\n".join(chunk)}
    return _err(f"Файл не найден ни в одном источнике: {rel_path}")


# --------------------------------------------------------------------------- #
# Поиск
# --------------------------------------------------------------------------- #

@mcp.tool()
def search_code(pattern: str, kinds: list[str] | None = None, name_filter: str = "",
                regex: bool = True, case_sensitive: bool = False, max_results: int = 100,
                source: str = "") -> dict:
    """Полнотекстовый поиск по BSL-модулям (ripgrep; без rg — Python-фолбэк).

    kinds — ограничить видами (['CommonModule','Document']); name_filter — подстрока
    имени объекта-владельца; source — один источник."""
    ws = _ws()
    kindset = set(kinds) if kinds else None
    if kindset and (bad := kindset - set(TYPE_FOLDERS.values())):
        return _err(f"Неизвестные виды: {', '.join(sorted(bad))}")
    return search.search_code(
        ws, pattern, kinds=kindset, name_filter=name_filter, regex=regex,
        case_sensitive=case_sensitive, max_results=max_results, source=source,
    )


@mcp.tool()
def fts_search(query: str, limit: int = 20, unit: str = "", source: str = "") -> dict:
    """Ранжированный поиск (SQLite FTS5, BM25) по рутинам и карточкам объектов:
    CamelCase-подслова, вес имени выше тела, кириллица матчится с усечением окончаний.

    unit: 'routine' | 'object' | пусто (всё). Требует построенного индекса (кнопка в
    админке или serve-lite --build-fts); свежесть — авто-дообновление по mtime раз в
    ~30 с, момент построения — в поле built_at. Это лексический ранжированный поиск,
    не семантика: синонимию без общих слов ловит только большой сервер (векторы)."""
    return fts.index_for(_ws()).search(query, limit=limit, unit=unit, source=source)


@mcp.tool()
def find_routine(routine_name: str, exported_only: bool = False, max_results: int = 50,
                 source: str = "") -> dict:
    """Где ОБЪЯВЛЕНА процедура/функция с этим именем (по всем источникам, точный парс)."""
    return code_intel.find_declarations(
        _ws(), routine_name, exported_only=exported_only, max_results=max_results, source=source,
    )


@mcp.tool()
def search_metadata(query: str, kinds: list[str] | None = None, max_results: int = 100,
                    source: str = "") -> dict:
    """Поиск объектов по имени и по тексту метаданных (синонимы и пр.).

    Сначала совпадения по имени (список объектов), затем текстовые совпадения в
    файлах метаданных (.xml/.mdo) с привязкой к объекту."""
    ws = _ws()
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

@mcp.tool()
def find_callees(kind: str, name: str, routine_name: str, module: str = "Module",
                 source: str = "") -> dict:
    """Кого вызывает рутина: разрешённые вызовы (local/common_module/manager) + неразрешённые."""
    return code_intel.find_callees(_ws(), kind, name, module, routine_name, source)


@mcp.tool()
def find_callers(routine_name: str, object_hint: str = "", kinds: list[str] | None = None,
                 max_results: int = 100, source: str = "") -> dict:
    """Места ВЫЗОВА рутины (проверено парсером: объявления и строки/комментарии исключены).

    object_hint — имя общего модуля/объекта для отсечения одноимённых методов."""
    return code_intel.find_callers(
        _ws(), routine_name, object_hint=object_hint, kinds=kinds,
        max_results=max_results, source=source,
    )


@mcp.tool()
def call_graph(routine_name: str, depth: int = 2, max_per_level: int = 40,
               source: str = "") -> dict:
    """Восходящий граф вызовов: кто (рекурсивно) вызывает рутину; уровни с охватывающими рутинами."""
    return code_intel.call_graph(
        _ws(), routine_name, depth=depth, max_per_level=max_per_level, source=source,
    )


@mcp.tool()
def find_overrides(kind: str = "", name: str = "", method: str = "", source: str = "") -> dict:
    """Переопределения расширений (&Вместо/&Перед/&После/&ИзменениеИКонтроль) с целями.

    Фильтры: kind+name — заимствованный объект; method — базовый метод; source — расширение."""
    return code_intel.find_overrides(_ws(), kind=kind, name=name, method=method, source=source)


@mcp.tool()
def find_handlers(kind: str, name: str, source: str = "") -> dict:
    """Обработчики объекта: события форм (+объявлен ли обработчик) и точки входа модулей
    (проведение/запись/проверка_заполнения/...)."""
    if err := _kind_ok(kind):
        return _err(err)
    return code_intel.find_handlers(_ws(), kind, name, source)


@mcp.tool()
def writes_to(document: str = "", register: str = "", source: str = "") -> dict:
    """Движения: document='Заказ' -> его регистры; register='ОстаткиТоваров' -> кто в него пишет."""
    return code_intel.writes_to(_ws(), document=document, register=register, source=source)


@mcp.tool()
def metrics(source: str = "") -> dict:
    """Инвентарь рабочей копии: объекты по видам, файлы/байты кода, число рутин, overrides."""
    return code_intel.metrics(_ws(), source=source)


# --------------------------------------------------------------------------- #
# Зависимости (метаданные)
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_dependencies(kind: str, name: str, source: str = "") -> dict:
    """Связи объекта: исходящие (ссылочные реквизиты по всем источникам, владельцы,
    движения) и входящие (кто ссылается на тип, подписки на события; для регистров —
    какие документы пишут). Метаданные-уровень; использование в коде — search_code."""
    if err := _kind_ok(kind):
        return _err(err)
    return code_intel.get_dependencies(_ws(), kind, name, source)


@mcp.tool()
def find_type_usages(kind: str, name: str, max_results: int = 100, source: str = "") -> dict:
    """Где используется ТИП объекта в метаданных: реквизиты объектов и форм, подписки,
    определяемые типы — точные строки файлов (`<Вид>Ref.<Имя>`/`<Вид>Object.<Имя>`)."""
    if err := _kind_ok(kind):
        return _err(err)
    return code_intel.type_usages(_ws(), kind, name, max_results=max_results, source=source)


# --------------------------------------------------------------------------- #
# Git-осведомлённость: изменения рабочей копии
# --------------------------------------------------------------------------- #

@mcp.tool()
def changed_objects(ref: str = "", source: str = "") -> dict:
    """Что изменено в рабочей копии: git status (ref пуст) или diff против ref
    (ветка/коммит/'HEAD~1'), сгруппировано по объектам метаданных.

    У каждого изменения — файл, git-статус и вид артефакта (module/meta/form_layout)."""
    return gitview.changed_objects(_ws(), ref, source)


@mcp.tool()
def review_set(ref: str = "", max_callers: int = 8, source: str = "") -> dict:
    """Ревью-набор изменений: изменённые строки → затронутые рутины → их вызывающие,
    точки входа и override-хуки расширений поверх них.

    Отвечает на «что я сломал этой правкой»: каждый вызывающий проверен парсером,
    untracked-модули включаются целиком. ref как в changed_objects."""
    return gitview.review_set(_ws(), ref, max_callers=max_callers, source=source)


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


@mcp.tool()
def get_service(name: str, source: str = "") -> dict:
    """Интроспекция сервиса: HTTPService (rootURL, шаблоны URL, методы) или WebService
    (namespace, операции с параметрами). Обработчики сверяются с модулем сервиса
    (declared/lines) — сразу видно, какие методы не реализованы."""
    ws = _ws()
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


@mcp.tool()
def get_form(kind: str, name: str, form: str = "", source: str = "") -> dict:
    """Структура формы: реквизиты, команды (+обработчики), элементы (поля с dataPath,
    кнопки с командами, группы), обработчики событий формы/элементов с пометкой declared.

    Для CommonForm параметр form не нужен. Форма расширения затеняет одноимённую базовую;
    секции, которых в форме расширения нет (реквизиты/команды), дополняются из базовой
    формы с пометкой attributes_source/commands_source — как их видит платформа."""
    ws = _ws()
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

@mcp.tool()
def platform_versions() -> dict:
    """Настроенные сборки справки платформы: версии, файлы .hbk, число тем.

    topics = null, пока индекс не построен (строится при первом запросе или кнопкой в админке)."""
    return _help().versions()


@mcp.tool()
def platform_docinfo(name: str, platform_version: str = "") -> dict:
    """Синтаксис-помощник: точный лукап темы по каноническому имени — русскому
    («Массив.Найти»), английскому («Array.Find») или короткому («Найти», с дизамбигуацией).

    platform_version сужает до конкретной сборки. Первый вызов строит индекс имён
    (десятки секунд на версию), дальше — мгновенно."""
    return _help().docinfo(name, platform_version)


@mcp.tool()
def platform_get_document(name: str, platform_version: str = "") -> dict:
    """Полный текст темы справки по точному имени («Объект.Метод») или fqn
    `platform_help:<версия>|<Имя>`. Без версии берётся самая свежая сборка."""
    return _help().get_document(name, platform_version)


@mcp.tool()
def platform_search(query: str, platform_version: str = "", limit: int = 20) -> dict:
    """Поиск по НАЗВАНИЯМ тем справки (подстрока, RU/EN) — навигация по API платформы.

    Семантический поиск по содержимому справки — у большого onec-vecgraph сервера."""
    return _help().search_titles(query, platform_version, limit)


# --------------------------------------------------------------------------- #
# Веб-админка (opt-in: ONEC_LITE_ADMIN=true / serve-lite --admin; только http-транспорт)
# --------------------------------------------------------------------------- #

def _admin_enabled() -> bool:
    return os.environ.get("ONEC_LITE_ADMIN", "").strip().lower() in ("1", "true", "yes", "on")


def _snapshot() -> dict:
    _init_rg_from_state()
    if _WS is None:
        try:
            _ws()  # env/state могут уже указывать на рабочую копию (запуск с --root)
        except RuntimeError:
            pass  # честно «не настроен» — пути задаются формой админки
    snap = lite_admin.workspace_snapshot(_WS)
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
        fts.index_for(_WS).status() if _WS is not None
        else {"available": fts.fts_available(), "built": False}
    )
    return snap


def apply_admin_paths(
    root: str, ext_text: str, help_text: str = "", rg_text: str | None = None
) -> tuple[dict | None, str | None]:
    """Re-point workspace + platform help (+ ripgrep path) and persist; (snapshot, errors).

    Частичный успех допустим (кривая строка справки не отменяет рабочую копию); в state
    сохраняется ФАКТИЧЕСКИ применённое, а не введённое — битый путь не переживёт рестарт."""
    _init_rg_from_state()
    root = (root or "").strip().strip('"').strip()
    ext = lite_admin.parse_ext_roots(ext_text)
    help_entries = platform_help.parse_help_lines(help_text)
    errors: list[str] = []
    if rg_text is not None:
        cleaned = rg_text.strip().strip('"')
        if cleaned and not Path(cleaned).is_file():
            errors.append(f"ripgrep: файл не найден: {cleaned}")
        else:
            search.set_rg_path(cleaned or None)  # пусто = вернуться к автопоиску
    if not root and not help_entries and rg_text is None:
        return None, "Укажите корень конфигурации и/или пути к справке платформы."
    if root:
        try:
            configure(root, tuple(ext))
        except Exception as exc:  # noqa: BLE001 - показать причину, оставив прежний workspace
            errors.append(f"Рабочая копия: {exc}")
    errors.extend(f"Справка: {e}" for e in configure_help(help_entries))
    try:
        lite_admin.save_paths(
            lite_admin.state_file(),
            str(_WS.root) if _WS is not None else "",
            list(_WS.ext_roots) if _WS is not None else [],
            platform_help=_HELP.entries,
            rg_path=search.rg_override() or "",
        )
    except OSError as exc:
        errors.append(f"Состояние не сохранено: {exc}")
    return _snapshot(), ("; ".join(errors) if errors else None)


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
        if action == "refresh":
            if _WS is not None:
                _WS.refresh()
            code_intel.clear_caches()
            _help().refresh()
            return RedirectResponse("admin?msg=" + quote("Кэши сброшены"), status_code=303)
        if action == "build_fts":
            if _WS is None:
                return RedirectResponse(
                    "admin?err=" + quote("Сначала задайте рабочую копию."), status_code=303)
            res = fts.index_for(_WS).build()
            if "error" in res:
                return RedirectResponse("admin?err=" + quote(res["error"]), status_code=303)
            msg = (f"Индекс поиска: +{res['files_added']} файлов, ~{res['files_updated']} "
                   f"обновлено, -{res['files_removed']}; юнитов записано "
                   f"{res['units_written']} за {res['seconds']} с (всего {res.get('units')})")
            return RedirectResponse("admin?msg=" + quote(msg), status_code=303)
        if action == "build_help":
            cat = _help()
            if not cat.entries:
                return RedirectResponse(
                    "admin?err=" + quote("Сначала задайте и примените пути к справке."),
                    status_code=303,
                )
            from time import perf_counter

            t0 = perf_counter()
            topics = len(cat.index())
            msg = f"Индекс справки построен: {topics} тем за {perf_counter() - t0:.1f} с"
            return RedirectResponse("admin?msg=" + quote(msg), status_code=303)
        _snap, err = apply_admin_paths(
            str(form.get("root") or ""),
            str(form.get("ext_roots") or ""),
            str(form.get("help_paths") or ""),
            rg_text=str(form.get("rg_path") or ""),
        )
        if err:
            return RedirectResponse("admin?err=" + quote(err), status_code=303)
        return RedirectResponse(
            "admin?msg=" + quote("Пути применены и сохранены"), status_code=303
        )
    return HTMLResponse(lite_admin.render_admin_page(
        _snapshot(),
        rg=search.rg_path(),
        state_path=str(lite_admin.state_file()),
        message=request.query_params.get("msg", ""),
        error=request.query_params.get("err", ""),
    ))


@mcp.custom_route("/admin.json", methods=["GET"])
async def admin_json(request: Request) -> Response:
    """Машиночитаемое состояние воркспейса (то же, что на странице)."""
    if not _admin_enabled():
        return JSONResponse({"error": "admin disabled"}, status_code=404)
    return JSONResponse(_snapshot())


def run(transport: str = "stdio") -> None:
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http")
