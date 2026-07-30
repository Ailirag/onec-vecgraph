"""Code intelligence over a live working copy: the hybrid "rg narrows, parser confirms" core.

Every answer is produced in two steps: ripgrep (or the Python fallback) finds candidate
files fast, then the project's BSL parser re-parses just those files (mtime-cached) so
results are routine-precise — enclosing routine, verified call sites, override
annotations, entry points — instead of raw text lines. No Neo4j, no embeddings.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ..bsl.parser import Routine, parse_module
from ..chunking import classify_entry_point
from ..parsing.dump import CODE_FOLDERS, TYPE_FOLDERS
from ..parsing.forms import parse_form_handlers
from . import search
from .workspace import LiteSource, Workspace, read_text

_KW = r"(?:Процедура|Функция|Procedure|Function)"

# Parsed-module cache: path -> (mtime, routines). Safe across workspaces (path is absolute).
# The name index for qualifier resolution lives in Workspace.name_index(): configuration
# names are NOT unique across repositories, so a module-level cache would collide.
_MODULES: dict[str, tuple[float, list[Routine]]] = {}
# Границы кэша: в HTTP-сервере на несколько воркспейсов он иначе растёт без предела (сотни
# тысяч рутин по 15k файлов на конфигурацию). Вытесняем самые старые вставки (FIFO ≈ LRU для
# обхода файлов), сохраняя дешёвую проверку свежести по mtime.
_MODULES_MAX = 8000

_MAX_CANDIDATE_FILES = 300  # cap on files re-parsed per callers query (быстрый режим)
_BY_OBJECT_TOP = 20  # сколько объектов показываем в сводке by_object (+ by_object_total)
_COMPLETE_MAX_HITS = 200_000  # предохранитель для complete-сканов (полный обход без обрезки)


def clear_caches() -> None:
    """Drop the parsed-module cache (admin refresh; mtime already guards staleness)."""
    _MODULES.clear()
    _LINES_CACHE.clear()
    _OVERRIDE_INDEX.clear()
    _DIRTY_CACHE.clear()
    _BEHIND_CACHE.clear()


def routines_of(path: Path) -> list[Routine]:
    """Parse a module file with an mtime-validated cache."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    hit = _MODULES.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    routines = parse_module(read_text(path))
    if len(_MODULES) >= _MODULES_MAX:
        for old in list(_MODULES)[: _MODULES_MAX // 4]:  # чистим пачкой, а не по одному
            _MODULES.pop(old, None)
    _MODULES[key] = (mtime, routines)
    return routines


_LINES_CACHE: dict[str, tuple[float, list[str]]] = {}
_LINES_CACHE_MAX = 64  # строки нужны пачками по одному модулю; держим последние


def _module_lines(path: Path) -> list[str]:
    """Строки модуля с mtime-кэшем.

    Без кэша `_signature` читал и декодировал ФАЙЛ ЦЕЛИКОМ на КАЖДУЮ рутину: на крупнейшем
    общем модуле УТ (7.6 МБ, 1825 рутин) это 1825 чтений и 68 с на один list_routines."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    hit = _LINES_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        lines = read_text(path).splitlines()
    except OSError:
        return []
    if len(_LINES_CACHE) >= _LINES_CACHE_MAX:
        for old in list(_LINES_CACHE)[: _LINES_CACHE_MAX // 4]:
            _LINES_CACHE.pop(old, None)
    _LINES_CACHE[key] = (mtime, lines)
    return lines


def _signature(path: Path, rt: Routine) -> str:
    """Declaration line(s) of a routine up to the closing paren (max 5 lines)."""
    lines = _module_lines(path)
    if not lines:
        return rt.name
    out: list[str] = []
    depth = 0
    for line in lines[rt.start_line - 1 : rt.start_line + 4]:
        out.append(line.strip())
        depth += line.count("(") - line.count(")")
        if depth <= 0 and "(" in "".join(out):
            break
    return " ".join(out)


def routine_row(path: Path, rt: Routine) -> dict:
    return {
        "name": rt.name,
        "kind": rt.kind,
        "export": rt.export,
        "lines": [rt.start_line, rt.end_line],
        "region": rt.region,
        "directive": rt.directive,
        "override": (
            {"mode": rt.override_mode, "target": rt.override_target} if rt.override_mode else None
        ),
        "entry_point": classify_entry_point(rt.name),
        "signature": _signature(path, rt),
    }


def find_in_module(path: Path, routine_name: str) -> Routine | None:
    low = routine_name.lower()
    for rt in routines_of(path):
        if rt.name.lower() == low:
            return rt
    return None


def routine_body(path: Path, rt: Routine) -> str:
    lines = read_text(path).splitlines()
    return "\n".join(lines[rt.start_line - 1 : rt.end_line])


# --------------------------------------------------------------------------- #
# Path -> object descriptor
# --------------------------------------------------------------------------- #

def describe_bsl_path(src: LiteSource, rel: str) -> dict:
    """Map a source-relative .bsl path to {kind, object, module} (best effort)."""
    parts = rel.replace("\\", "/").split("/")
    kind = CODE_FOLDERS.get(parts[0], parts[0])
    name = parts[1] if len(parts) > 1 else ""
    if parts[0] == "Configuration":
        # Модули приложения/сеанса лежат прямо в папке: объекта-владельца нет, объект — сама
        # конфигурация, а имя файла (SessionModule и пр.) и есть модуль.
        return {"kind": "Configuration", "object": "Configuration",
                "module": Path(parts[-1]).stem}
    if parts[0] == "Subsystems":  # nested: Subsystems/A/Subsystems/B/...
        name = ".".join(p for p in parts[1:-1] if p not in ("Subsystems", "Ext"))
    module = Path(parts[-1]).stem
    if "Forms" in parts:
        form = parts[parts.index("Forms") + 1]
        module = f"Form:{form}"
    elif kind == "CommonForm" and module == "Module":
        module = "Form"
    return {"kind": kind, "object": f"{kind}.{name}", "module": module}


def artifact_of(rel: str) -> str:
    """Coarse artifact class of a source-relative file: module | form_layout | meta | other."""
    low = rel.replace("\\", "/").lower()
    if low.endswith(".bsl"):
        return "module"
    if low.endswith(".form") or low.endswith("/ext/form.xml"):
        return "form_layout"
    if low.endswith(".mdo") or re.fullmatch(r"[^/]+/[^/]+\.xml", low):
        return "meta"
    return "other"


def _meta_owner(rel: str) -> str | None:
    """Owner object fqn of a metadata/form file path ('Catalog.Товары'), best effort."""
    parts = rel.replace("\\", "/").split("/")
    kind = TYPE_FOLDERS.get(parts[0])
    if kind is None or len(parts) < 2:
        return None
    name = Path(parts[1]).stem
    return f"{kind}.{name}"


# --------------------------------------------------------------------------- #
# Type usages / dependencies (metadata-level)
# --------------------------------------------------------------------------- #

def type_usages(
    ws: Workspace, kind: str, name: str, *, max_results: int = 200, source: str = "",
    offset: int = 0,
) -> dict:
    """Где тип объекта упоминается в МЕТАДАННЫХ: реквизиты объектов и форм, подписки,
    определяемые типы. Матчит `<Вид>Ref.<Имя>` и `<Вид>Object.<Имя>` в .xml/.mdo/.form."""
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    pattern = rf"{kind}(?:Ref|Object)\.{re.escape(name)}\b"
    # Один текстовый проход на ВСЕ источники и все маски метаданных: раньше здесь было до
    # 10 запусков rg (5 источников × 2 маски), а каждый запуск на Windows стоит ~0.15 с.
    globs: list[str] = []
    for s in srcs:
        for g in (("*.mdo", "*.form") if s.fmt == "edt" else ("*.xml",)):
            if g not in globs:
                globs.append(g)
    # Собираем ВСЕ использования, потом отдаём окно: раньше обход прекращался на max_results и
    # ответ не сообщал ни полного счёта, ни offset — у ходового типа было видно 50 из 1129
    # (96% скрыто), и агент считал выдачу исчерпывающей.
    all_rows: list[dict] = []
    _engine, it = search.stream(
        ws, pattern, sources=srcs, kinds=None, glob=globs, regex=True,
        max_hits=_COMPLETE_MAX_HITS,
    )
    for abs_path, line_no, text in it:
        src_name, rel = ws.source_of_path(Path(abs_path))
        owner = _meta_owner(rel)
        if owner is None:
            continue
        all_rows.append({
            "source": src_name,
            "object": owner,
            "artifact": artifact_of(rel),
            "path": rel,
            "line": line_no,
            # 120 симв. хватает на строку объявления типа; полный контекст — read_file по path
            "text": text.strip()[:120],
        })
    by_object: dict[str, int] = {}
    for r in all_rows:
        by_object[r["object"]] = by_object.get(r["object"], 0) + 1
    start = max(0, offset)
    window = all_rows[start: start + max(1, max_results)]
    return {
        "type": f"{kind}.{name}",
        # Область действия обязана быть в ОТВЕТЕ, а не только в описании тула: имя
        # find_type_usages шире того, что тул делает, и агент, прочитавший лишь ответ, принимал
        # `usage_count` за все использования типа, включая обращения из кода.
        "scope": "metadata",
        "scope_note": ("Только метаданные (.mdo/.xml/.form): реквизиты, подписки, определяемые "
                       "типы. Обращения из КОДА (`Справочники.<Имя>`, менеджеры, запросы) здесь "
                       "НЕ считаются — для них find_callers по методу или rg по тексту."),
        "usage_count": len(all_rows),          # ВСЕГО использований в метаданных
        "match_count": len(window),
        "offset": start,
        "truncated": start + len(window) < len(all_rows),
        "by_object": [{"object": o, "count": n}
                      for o, n in sorted(by_object.items(), key=lambda kv: -kv[1])[:40]],
        "usages": window,
    }


def subscriptions_for(ws: Workspace, kind: str, name: str, source: str = "") -> list[dict]:
    """Подписки на события, у которых источник — данный объект (разбор EventSubscription)."""
    srcs, _err = ws.resolve_sources(source)
    low = name.lower()
    out: list[dict] = []
    for s in srcs:
        for fqn, meta, obj_dir in ws.listing(s):
            if not fqn.startswith("EventSubscription."):
                continue
            try:
                obj = ws.parse_object(s, (fqn, meta, obj_dir))
            except ValueError:
                continue
            hit = any(
                t.ref_kind == kind and (t.ref_name or "").lower() == low
                for t in obj.source_types
            )
            if hit:
                out.append({
                    "source": s.name,
                    "subscription": obj.name,
                    "event": obj.event,
                    "handler": obj.handler_raw,
                })
    return out


def get_dependencies(ws: Workspace, kind: str, name: str, source: str = "") -> dict:
    """Связи объекта: исходящие (ссылочные реквизиты, владельцы, движения) из метаданных
    всех источников (заимствованный объект = base + ext) и входящие (кто ссылается на
    тип, кто подписан, для регистров — кто пишет)."""
    cands, err = ws.find_objects(kind, name, source)
    if err:
        return {"error": err}
    refs: dict[str, list[str]] = {}
    owners: list[str] = []
    register_records: list[str] = []
    for src, ref in cands:
        try:
            obj = ws.parse_object(src, ref)
        except ValueError:
            continue
        for f in obj.fields:
            for t in f.types:
                if t.ref_fqn:
                    refs.setdefault(t.ref_fqn, []).append(f.name)
        for ts in obj.tabular:
            for f in ts.fields:
                for t in f.types:
                    if t.ref_fqn:
                        refs.setdefault(t.ref_fqn, []).append(f"{ts.name}.{f.name}")
        owners.extend(o for o in obj.owners if o not in owners)
        register_records.extend(r for r in obj.register_records if r not in register_records)

    # max_results=0 -> обходим ВСЕ использования: раньше срез в 500 строк давал `matches` по
    # объекту, посчитанный на неполной выборке, и подавался как факт. Само окно выдачи здесь
    # не нужно — из usages строится только агрегат по объектам.
    usages = type_usages(ws, kind, name, max_results=10**9, source=source)
    self_fqn = f"{kind}.{name}"
    by_obj: dict[tuple[str, str], int] = {}
    for u in usages.get("usages", []):
        if u["object"] == self_fqn and u["artifact"] != "meta":
            continue  # собственные формы объекта — не «входящая» зависимость
        key = (u["source"], u["object"])
        by_obj[key] = by_obj.get(key, 0) + 1
    referenced_by = [
        {"source": s, "object": o, "matches": n}
        for (s, o), n in sorted(by_obj.items(), key=lambda kv: (-kv[1], kv[0]))
        if o != self_fqn
    ]

    out: dict = {
        "object": self_fqn,
        "source": cands[0][0].name,
        "also_in": [s.name for s, _ in cands[1:]],
        "references": [
            {"target": target, "attributes": sorted(set(attrs))}
            for target, attrs in sorted(refs.items())
        ],
        "referenced_by": referenced_by,
        "referenced_by_truncated": usages.get("truncated", False),
        "subscriptions": subscriptions_for(ws, kind, name, source),
    }
    if owners:
        out["owners"] = owners
    if register_records:
        out["register_records"] = register_records
    if kind.endswith("Register"):
        writers = writes_to(ws, register=name, source=source)
        out["written_by"] = [w["document"] for w in writers.get("writers", [])]
    return out


# --------------------------------------------------------------------------- #
# Qualifier resolution (callees)
# --------------------------------------------------------------------------- #

def _resolve_call(
    ws: Workspace, qualifier: str | None, method: str, local: dict[str, Routine],
    home: tuple[LiteSource, Path],
) -> dict:
    """Classify one parsed call the way the big call-grapher does (local/common/manager)."""
    row: dict = {"call": (f"{qualifier}." if qualifier else "") + method}
    if qualifier is None:
        rt = local.get(method.lower())
        if rt:
            src, path = home
            row.update(kind="local", confidence="high", source=src.name,
                       path=ws.source_of_path(path)[1], routine=rt.name,
                       lines=[rt.start_line, rt.end_line])
        else:
            row.update(kind="unresolved",
                       note="не локальный: платформенный/глобальный вызов?")
        return row
    for src in ws.sources:
        candidates = ws.name_index(src).get(qualifier.lower(), [])
        for kind, fqn in candidates:
            name = fqn.partition(".")[2]
            stem = "Module" if kind == "CommonModule" else "ManagerModule"
            ref = ws._require_ref(src, kind, name)  # noqa: SLF001 - hot path, listing already cached
            if ref[2] is None:
                continue
            mpath = ws._module_file(src, ref[2], stem)  # noqa: SLF001
            if mpath is None:
                continue
            target = find_in_module(mpath, method)
            if target is None:
                continue
            row.update(
                kind="common_module" if kind == "CommonModule" else "manager",
                confidence="high" if kind == "CommonModule" else "medium",
                source=src.name, target=f"{fqn}::{target.name}",
                path=ws.source_of_path(mpath)[1], export=target.export,
                lines=[target.start_line, target.end_line],
            )
            return row
    row.update(kind="unresolved", note=f"'{qualifier}' не разрешён в общий/менеджерский модуль")
    return row


def find_callees(
    ws: Workspace, kind: str, name: str, module: str, routine_name: str, source: str = ""
) -> dict:
    cands, err = ws.find_objects(kind, name, source)
    if err:
        return {"error": err}
    # Adopted objects live in several sources: the extension's module holds only its own
    # hooks, so fall through to the first source whose module declares the routine.
    first_msg: str | None = None
    seen_modules: list[tuple[LiteSource, Path]] = []
    for src, ref in cands:
        mpath, msg = ws.module_path(src, kind, ref[0].partition(".")[2], module)
        if mpath is None:
            first_msg = first_msg or msg
            continue
        seen_modules.append((src, mpath))
        rt = find_in_module(mpath, routine_name)
        if rt is None:
            continue
        local = {r.name.lower(): r for r in routines_of(mpath)}
        # «Что вызывает эта рутина» — это МНОЖЕСТВО адресатов, поэтому повторные вхождения
        # одного и того же вызова схлопываем здесь, на выдаче. В самих данных (calls) хранится
        # каждое вхождение: иначе теряются места вызова, см. bsl.parser._find_calls.
        uniq: dict[tuple[str | None, str], object] = {}
        for c in rt.calls:
            uniq.setdefault(((c.qualifier or "").lower() or None, c.method.lower()), c)
        calls = [_resolve_call(ws, c.qualifier, c.method, local, (src, mpath))
                 for c in uniq.values()]
        resolved = [c for c in calls if c["kind"] != "unresolved"]
        return {
            "source": src.name,
            "routine": rt.name,
            "module": module,
            "object": f"{kind}.{name}",
            "also_in": [s.name for s, _ in cands if s.name != src.name],
            "calls_total": len(calls),
            "resolved": resolved,
            "unresolved": [c for c in calls if c["kind"] == "unresolved"],
        }
    if seen_modules:
        checked = ", ".join(s.name for s, _ in seen_modules)
        avail = ", ".join(r.name for r in routines_of(seen_modules[0][1])[:20])
        return {"error": f"Рутина '{routine_name}' не найдена в модуле '{module}' "
                         f"(источники: {checked}). Есть: {avail}"}
    return {"error": first_msg or f"Модуль '{module}' не найден у {kind}.{name}."}


# --------------------------------------------------------------------------- #
# Callers / call graph (rg narrows -> parser confirms)
# --------------------------------------------------------------------------- #

def _candidate_files(
    ws: Workspace, pattern: str, sources: list[LiteSource], kinds: set[str] | None,
    cap: int = _MAX_CANDIDATE_FILES, complete: bool = False,
) -> tuple[list[Path], bool]:
    """Distinct files with a text-level match; (files, truncated).

    complete=False — поток rg с ранним выходом по cap (быстро, но порядок обхода у rg
    параллельный, поэтому набор при обрезке НЕ детерминирован). complete=True — поток
    вычитывается до конца и файлы сортируются: ответ воспроизводим и полон. Для сканов, чей
    результат агент считает исчерпывающим (переопределения расширений), обязателен режим
    complete — иначе одинаковый запрос даёт разные ответы."""
    _engine, it = search.stream(
        ws, pattern, sources=sources, kinds=kinds, regex=True,
        max_hits=(_COMPLETE_MAX_HITS if complete else cap * 40),
    )
    files: list[Path] = []
    seen: set[str] = set()
    truncated = False
    hits = 0
    for abs_path, _line, _text in it:
        hits += 1
        if abs_path in seen:
            continue
        seen.add(abs_path)
        files.append(Path(abs_path))
        if not complete and len(files) >= cap:
            truncated = True
            break
    if complete:
        files.sort()
        # Предохранитель исчерпан -> обход НЕ полон, и об этом надо сказать: раньше флаг
        # выставлялся только в ветке с кэпом, поэтому complete-скан на массовом шаблоне
        # («Если» даёт свыше 400 тыс. попаданий) молча возвращал truncated=False.
        if hits >= _COMPLETE_MAX_HITS:
            truncated = True
    return files, truncated


def _enclosing(routines: list[Routine], line: int) -> Routine | None:
    for rt in routines:
        if rt.start_line <= line <= rt.end_line:
            return rt
    return None


def find_callers(
    ws: Workspace, routine_name: str, *, object_hint: str = "", kinds: list[str] | None = None,
    max_results: int = 100, source: str = "", summary_only: bool = False,
) -> dict:
    """Call sites of a routine by name: parser-verified, declarations excluded.

    object_hint narrows qualified calls to `Хинт.Метод(...)` (плюс неквалифицированные
    вызовы внутри модулей самого объекта/модуля Хинт). summary_only=True — только сводка
    (полный счёт + разбивка по объектам) без строк: на «горячих» методах конфигурации это
    десятки токенов вместо тысяч.
    """
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    # Индексный путь (если индекс символов готов): полный счёт мест вызова без обрезки по
    # файлам-кандидатам и без текстового скана. kinds сужают выборку прямо в SQL.
    kindset = set(kinds) if kinds else None
    merge: dict[str, dict[str, int]] = {}
    indexed = _index_callers(ws, [routine_name], max_per_name=max_results, source=source,
                             kinds=kindset, hint=object_hint, merge_out=merge)
    # «Нет вызывающих» — самый дорогой неверный ответ (агент решит, что правка безопасна), но
    # подтверждать сканом КАЖДЫЙ пустой ответ слишком дорого: у 27% имён вызовов правда нет, и
    # они платили полный обход (до 14 с). Скан нужен, только если индекс имени НЕ ЗНАЕТ — то
    # есть его могли добавить после сборки; известное имя с нулём вызовов — корректный ноль.
    if indexed is not None and not indexed.get(routine_name) and not _index_knows(ws, routine_name):
        indexed = None
    if indexed is not None:
        rows = indexed.get(routine_name, [])  # хинт уже применён в SQL, а не к окну выдачи
        hint_low = object_hint.lower()
        # Сводка и счёт берутся по ВСЕМУ множеству (одним GROUP BY), а не по окну выдачи:
        # иначе by_object показывал распределение по первым по алфавиту объектам, и агент
        # планировал по 2% данных. Фильтры source/kinds входят в сам запрос, поэтому счёт
        # всегда соответствует отданным строкам. object_hint — фильтр по квалификатору
        # вызова, в SQL его нет, поэтому при нём счётчики не заявляем.
        src_names = {s.name for s in srcs} if source else None
        stats = _index_call_stats(ws, routine_name, source_names=src_names, kinds=kindset,
                                  hint=hint_low)
        rows_total = stats.get("rows")
        by_object = list(stats.get("by_object") or [])
        distinct = stats.get("distinct_callers")
        # Счётчики берутся из SQL, а строки — уже с подмешанной ЖИВОЙ работой. Если подмешивание
        # что-то изменило, сырые агрегаты противоречат выдаче: на незакоммиченной правке было
        # `match_count: 3` при `call_rows_total: 1` и сумме by_object == 1 без всяких флагов —
        # то есть ровно в главном сценарии инструмента счёт был меньше показанных строк.
        delta = merge.get("added", {}).get(routine_name, 0) - \
            merge.get("dropped", {}).get(routine_name, 0)
        if delta or merge.get("dropped", {}).get(routine_name, 0):
            if len(rows) < max_results:
                # Набор полный (индекс отдал меньше лимита) — считаем прямо по нему, это точно.
                rows_total = len(rows)
                agg: dict[str, int] = {}
                for r in rows:
                    agg[r.get("object") or "?"] = agg.get(r.get("object") or "?", 0) + 1
                by_object = [{"object": k, "count": v}
                             for k, v in sorted(agg.items(), key=lambda kv: -kv[1])]
                distinct = len({(r.get("object"), r.get("routine")) for r in rows})
            elif rows_total is not None:
                rows_total = max(rows_total + delta, len(rows))
        return {
            "routine": routine_name,
            "match_count": len(rows),
            # Незакоммиченная работа подмешана живым разбором: агрегаты пересчитаны по выдаче
            # (полный набор) или скорректированы на разницу (обрезанный).
            "uncommitted_merged": bool(delta or merge.get("dropped", {}).get(routine_name, 0)),
            # честные имена: в calls хранится по одной записи на (рутина, квалификатор, метод),
            # поэтому это «строк вызова», а не «текстовых вхождений»
            "call_rows_total": rows_total,
            "distinct_callers": distinct,
            "truncated": bool(rows_total and len(rows) < rows_total),
            "engine": "index",
            # Сводка по объектам — по всему множеству; агенту её обычно достаточно, чтобы
            # решить, куда смотреть, без вычитывания строк вызовов.
            # Сводку тоже надо ограничивать: у платформенного хука это 810 объектов и
            # ~19.7 тыс. токенов на ДЕФОЛТНОМ вызове, ровно там, где обещаны «десятки».
            # Обрезка помечается явно И числами: без этого сумма by_object не сходилась с
            # call_rows_total (887 против 2323), и агент не мог понять, что видит верхушку.
            "by_object": by_object[:_BY_OBJECT_TOP],
            "by_object_total": len(by_object),
            "by_object_truncated": len(by_object) > _BY_OBJECT_TOP,
            "by_object_rows_shown": sum(
                x["count"] for x in by_object[:_BY_OBJECT_TOP]),
            "callers": [] if summary_only else rows,
        }
    pattern = rf"\b{re.escape(routine_name)}\s*\("
    # Без индекса идём ПОЛНЫМ обходом: кэп в 300 файлов-кандидатов молча срезал две трети мест
    # вызова (на УТ 81 вместо 248), и ответ было не отличить от исчерпывающего. Полнота
    # приоритетнее скорости — здесь complete=True.
    files, files_truncated = _candidate_files(ws, pattern, srcs, kindset, complete=True)
    rows: list[dict] = []
    hint = object_hint.lower()
    truncated = False
    for path in files:
        routines = routines_of(path)
        src_name, rel = ws.source_of_path(path)
        src = next((s for s in ws.sources if s.name == src_name), None)
        descr = describe_bsl_path(src, rel) if src else {"module": "?", "object": "?"}
        in_hint_object = bool(hint) and hint in descr["object"].lower()
        for rt in routines:
            for call in rt.calls:
                if call.method.lower() != routine_name.lower():
                    continue
                # Пропускаем только НЕквалифицированный самовызов; `Модуль.ОдноимённыйМетод()`
                # — реальное место вызова (штатная делегация обработчика в общий модуль).
                if call.qualifier is None and rt.name.lower() == routine_name.lower():
                    continue
                if hint:
                    q = (call.qualifier or "").lower()
                    if not (q == hint or (call.qualifier is None and in_hint_object)):
                        continue
                # Unqualified call that resolves to a same-file routine is local — it only
                # targets the searched name if that local IS the searched routine's module,
                # which object_hint captures; keep and label instead of guessing.
                rows.append({
                    "source": src_name,
                    "path": rel,
                    **{k: v for k, v in descr.items() if k in ("object", "module")},
                    "routine": rt.name,
                    "routine_lines": [rt.start_line, rt.end_line],
                    "qualifier": call.qualifier,
                    "call_line": call.line or None,  # точная строка вызова (не только рутина)
                    "local_target": call.qualifier is None
                    and any(r.name.lower() == routine_name.lower() for r in routines),
                })
                if len(rows) >= max_results:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    by_object_scan: dict[str, int] = {}
    for r in rows:
        by_object_scan[r.get("object") or "?"] = by_object_scan.get(r.get("object") or "?", 0) + 1
    return {
        "routine": routine_name,
        "match_count": len(rows),
        # На скан-пути полного счёта нет (обход останавливается на max_results), поэтому счётчик
        # не выдумываем; зато честно говорим, каким движком отвечали и сколько файлов прочитали —
        # раньше по ответу нельзя было понять, полон он или это треть множества.
        "call_rows_total": None,
        "engine": "scan",
        "files_scanned": len(files),
        "files_truncated": files_truncated,
        "truncated": truncated or files_truncated,
        "by_object": [{"object": o, "count": n}
                      for o, n in sorted(by_object_scan.items(), key=lambda kv: -kv[1])],
        "callers": rows,
    }


def _index_callers_grouped(
    ws: Workspace, names: list[str], *, max_per_name: int, source: str,
    hints: dict[str, str],
) -> dict[str, list[dict]] | None:
    """Вызывающие для набора имён, у каждого СВОЙ хинт (нужно review_set / call_graph).

    Хинт выражается в SQL, а он один на запрос — поэтому имена группируются по значению хинта
    (в ревью-наборе это имя общего модуля, и на десятки рутин приходятся единицы модулей).
    Без этого индексный путь игнорировал хинты вовсе и приписывал изменённой рутине чужих
    одноимённых вызывающих из других модулей."""
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault((hints.get(n) or hints.get(n.lower()) or ""), []).append(n)
    out: dict[str, list[dict]] = {}
    for hint, group in groups.items():
        part = _index_callers(ws, group, max_per_name=max_per_name, source=source, hint=hint)
        if part is None:
            return None  # индекса нет — весь набор считаем сканом, иначе смешаем движки
        out.update(part)
    return out


def _index_callers(
    ws: Workspace, names: list[str], *, max_per_name: int, source: str,
    kinds: set[str] | None = None, hint: str = "",
    merge_out: dict[str, dict[str, int]] | None = None,
) -> dict[str, list[dict]] | None:
    """Вызывающие из индекса символов (None — индекса нет, работаем текстовым сканом).

    Импорт fts здесь, а не сверху: fts импортирует code_intel (наполняет индекс тем же
    разбором), верхний импорт замкнул бы цикл."""
    try:
        from . import fts as _fts
        idx = _fts.index_for(ws)
        if not idx.has_symbols():
            return None
        src_names = {s.name for s in ws.resolve_sources(source)[0]} if source else None
        found = idx.callers_of(names, max_per_name=max_per_name, kinds=kinds,
                               source_names=src_names, hint=hint)
    except Exception:  # noqa: BLE001 — индекс не должен ломать ответ, есть скан-фолбэк
        return None
    if source:
        keep = {s.name for s in ws.resolve_sources(source)[0]}
        found = {n: [r for r in rows if r.get("source") in keep] for n, rows in found.items()}
    # Свежую работу разработчика индекс по определению не видит, поэтому «грязный» набор
    # (изменённые и новые файлы по git) всегда разбирается ЖИВЫМ парсером и подмешивается.
    # Без этого индекс уверенно отвечал «0 вызывающих» на только что добавленный вызов: строки
    # по такому файлу в выборку не попадают вовсе, поэтому пометки `stale` для него не будет.
    dirty_paths = _dirty_bsl_files(ws) | _behind_index_files(ws)
    stale_paths = {
        r["_abs"] for rows in found.values() for r in rows if r.get("stale") and r.get("_abs")
    }
    live_paths = stale_paths | dirty_paths
    wanted = {n.lower(): n for n in names}
    dropped: dict[str, int] = {}
    added: dict[str, int] = {}
    for name, rows in found.items():
        kept = [r for r in rows if not r.get("stale") and r.get("_abs") not in live_paths]
        if len(kept) != len(rows):
            dropped[name] = len(rows) - len(kept)
        found[name] = kept
    # Индекс отстал — просим фоновый догон (неблокирующе), иначе долгоживущий http-сервер
    # мог работать по индексу произвольной давности: рефреш кикался только из fts.search().
    if live_paths:
        try:
            from . import fts as _fts2
            _fts2.index_for(ws).ensure_background()
        except Exception:  # noqa: BLE001
            pass
    for abs_path in live_paths:
        path = Path(abs_path)
        src_name, rel = ws.source_of_path(path)
        src = next((s for s in ws.sources if s.name == src_name), None)
        if src is None:
            continue
        descr = describe_bsl_path(src, rel)
        local_names = {r.name.lower() for r in routines_of(path)}
        for rt in routines_of(path):
            for call in rt.calls:
                target = wanted.get(call.method.lower())
                if target is None:
                    continue
                if call.qualifier is None and rt.name.lower() == target.lower():
                    continue  # только неквалифицированный самовызов, см. callers_of
                if len(found[target]) >= max_per_name:
                    continue
                added[target] = added.get(target, 0) + 1
                found[target].append({
                    "source": src_name, "path": rel,
                    **{k: v for k, v in descr.items() if k in ("object", "module")},
                    "routine": rt.name, "routine_lines": [rt.start_line, rt.end_line],
                    "export": rt.export, "qualifier": call.qualifier,
                    "call_line": call.line or None,
                    "local_target": call.qualifier is None and target.lower() in local_names,
                })
    for rows in found.values():  # служебное поле склейки наружу не отдаём
        for r in rows:
            r.pop("_abs", None)
    if merge_out is not None:
        # Учёт подмешивания наружу: без него агрегаты из SQL противоречили выданным строкам.
        merge_out["dropped"] = dropped
        merge_out["added"] = added
    return found


_DIRTY_TTL = 3.0  # с: «грязный» набор — это то, что разработчик правит СЕЙЧАС
_DIRTY_CACHE: dict[str, tuple[float, set[str]]] = {}


_BEHIND_TTL = 60.0  # с: закоммиченное меняется редко, а range-diff дороже git status
_BEHIND_CACHE: dict[str, tuple[float, set[str]]] = {}


def _behind_index_files(ws: Workspace) -> set[str]:
    """Абсолютные пути .bsl, ЗАКОММИЧЕННЫЕ после сборки индекса символов.

    Живым разбором раньше подмешивалось только незакоммиченное (git status), а строк по
    закоммиченному файлу в выборке нет — значит и пометки stale для него не будет. Поэтому
    добавленный и закоммиченный вызов индекс уверенно показывал как отсутствующий: ответ
    `call_rows_total: 1` при двух реальных местах вызова, без каких-либо флагов. Сравниваем
    коммит сборки с текущим HEAD одним `git diff --name-only`."""
    key = str(ws.root).lower()
    now = time.monotonic()
    hit = _BEHIND_CACHE.get(key)
    if hit and now - hit[0] < _BEHIND_TTL:
        return hit[1]
    out: set[str] = set()
    try:
        from . import fts as _fts
        from . import gitview as _gv
        heads = _fts.indexed_heads(ws)
        if heads:
            for repo_str, sha in heads.items():
                repo = Path(repo_str)
                code, cur = _gv._git(["rev-parse", "HEAD"], repo)  # noqa: SLF001
                if code != 0 or not cur.strip() or cur.strip() == sha:
                    continue
                code, out_txt = _gv._git(  # noqa: SLF001
                    ["diff", "--name-only", f"{sha}..HEAD", "--", "*.bsl"], repo)
                if code != 0:
                    continue
                for rel in out_txt.splitlines():
                    rel = rel.strip().strip('"')
                    if rel.endswith(".bsl"):
                        out.add(str(repo / rel))
    except Exception:  # noqa: BLE001 — без git/индекса догонять нечего
        out = set()
    _BEHIND_CACHE[key] = (now, out)
    return out


def _dirty_bsl_files(ws: Workspace) -> set[str]:
    """Абсолютные пути .bsl, которые изменены или ещё не в git (незакоммиченная работа).

    Свой короткий TTL, а НЕ общий 60-секундный кэш git-списков: иначе файл, сохранённый после
    предыдущего вызова инструмента, оставался бы невидимым до минуты — ровно в сценарии
    «правлю код и спрашиваю агента». 3 с гасят стоимость подряд идущих вызовов и при этом
    не прячут свежую правку. Импорт gitview внутри функции: gitview импортирует code_intel."""
    key = str(ws.root).lower()
    now = time.monotonic()
    hit = _DIRTY_CACHE.get(key)
    if hit and now - hit[0] < _DIRTY_TTL:
        return hit[1]
    try:
        from . import gitview as _gv
        by_root, _missing = _gv._repos(ws.sources)  # noqa: SLF001 — общий внутренний слой
    except Exception:  # noqa: BLE001 — без git просто нет «грязного» набора
        return set()
    out: set[str] = set()
    for repo in by_root:
        try:
            files, err = _gv._status_files(repo)  # noqa: SLF001 — свежий статус, без TTL-кэша
        except Exception:  # noqa: BLE001
            continue
        if err:
            continue
        for _status, rel in files:
            if not rel.endswith(".bsl"):
                continue
            abs_path = repo / rel
            src_name, srel = ws.source_of_path(abs_path)
            if src_name and srel.split("/", 1)[0] in CODE_FOLDERS:
                out.add(str(abs_path))
    _DIRTY_CACHE[key] = (now, out)
    return out


def _merge_live_overrides(ws: Workspace, rows: list[dict]) -> list[dict]:
    """Заменить строки индекса по «грязным»/расходящимся файлам на живой разбор аннотаций.

    Иначе только что добавленный в расширении хук `&Вместо(...)` невидим для find_overrides и
    для `review_set.overridden_by` — то есть ревью не покажет перехват собственной правки."""
    dirty = _dirty_bsl_files(ws)
    suspect = {r["_abs"] for r in rows if r.get("_stale") and r.get("_abs")} | dirty
    kept = [r for r in rows if r.get("_abs") not in suspect]
    ext_names = {s.name for s in ws.sources if s.is_extension}
    for abs_path in suspect:
        path = Path(abs_path)
        src_name, rel = ws.source_of_path(path)
        if src_name not in ext_names:
            continue  # переопределения живут только в расширениях
        src = next((s for s in ws.sources if s.name == src_name), None)
        if src is None:
            continue
        descr = describe_bsl_path(src, rel)
        for rt in routines_of(path):
            if not rt.override_mode:
                continue
            kept.append({"source": src_name, "object": descr.get("object"),
                         "module": descr.get("module"), "path": rel, "routine": rt.name,
                         "mode": rt.override_mode, "target": rt.override_target,
                         "directive": rt.directive,
                         "lines": [rt.start_line, rt.end_line]})
    for r in kept:
        r.pop("_abs", None)
        r.pop("_stale", None)
    return kept


def _merge_live_declarations(ws: Workspace, rows: list[dict], routine_name: str,
                             exported_only: bool, srcs: list[LiteSource]) -> list[dict]:
    """Заменить строки индекса по «грязным»/расходящимся файлам на живой разбор."""
    dirty = _dirty_bsl_files(ws)
    suspect = {r["_abs"] for r in rows if r.get("_stale") and r.get("_abs")} | dirty
    kept = [r for r in rows if r.get("_abs") not in suspect]
    low = routine_name.lower()
    keep_names = {s.name for s in srcs}
    for abs_path in suspect:
        path = Path(abs_path)
        src_name, rel = ws.source_of_path(path)
        if src_name not in keep_names:
            continue
        src = next((s for s in ws.sources if s.name == src_name), None)
        if src is None:
            continue
        descr = describe_bsl_path(src, rel)
        for rt in routines_of(path):
            if rt.name.lower() != low or (exported_only and not rt.export):
                continue
            kept.append({"source": src_name, "object": descr.get("object"),
                         "module": descr.get("module"), "path": rel, "name": rt.name,
                         "lines": [rt.start_line, rt.end_line], "export": rt.export,
                         "directive": rt.directive})
    for r in kept:
        r.pop("_abs", None)
        r.pop("_stale", None)
    return kept


def _index_level_total(ws: Workspace, names: list[str], *, source: str = "") -> int | None:
    """Сколько мест вызова у набора имён всего (по индексу); None — индекса нет."""
    if not names:
        return 0
    try:
        from . import fts as _fts
        idx = _fts.index_for(ws)
        if not idx.has_symbols():
            return None
        src_names = {s.name for s in ws.resolve_sources(source)[0]} if source else None
        stats = idx.call_totals(names, source_names=src_names)
        return sum(int(v.get("rows") or 0) for v in stats.values())
    except Exception:  # noqa: BLE001
        return None


def _index_knows(ws: Workspace, name: str) -> bool:
    """Знает ли индекс это имя (объявление или вызов) — см. правило про пустые ответы."""
    try:
        from . import fts as _fts
        return _fts.index_for(ws).has_name(name)
    except Exception:  # noqa: BLE001
        return False


def _index_call_stats(ws: Workspace, name: str, *, source_names: set[str] | None = None,
                      kinds: set[str] | None = None, hint: str = "") -> dict:
    """Полная статистика вызовов из индекса: строки, различные вызывающие, разбивка по объектам.

    Считается по всему множеству с теми же фильтрами, что и выдача, — чтобы счётчики в ответе
    означали именно то, что написано (см. find_callers)."""
    try:
        from . import fts as _fts
        return _fts.index_for(ws).call_totals(
            [name], source_names=source_names, kinds=kinds, hint=hint).get(name, {})
    except Exception:  # noqa: BLE001
        return {}


def find_callers_batch(
    ws: Workspace, names: list[str], *, hints: dict[str, str] | None = None,
    max_per_name: int = 8, source: str = "",
) -> dict[str, list[dict]]:
    """Callers для НЕСКОЛЬКИХ рутин за ОДИН текстовый скан (имена через альтернацию).

    Раньше review_set/call_graph звали find_callers на каждое имя — это N полных проходов по
    конфигурации (на УТ десятки секунд). Здесь скан один, а каждый файл-кандидат парсится один
    раз (кэш по mtime) и вызовы раскладываются по запрошенным именам. Семантика строк —
    как у find_callers (объявления/комментарии исключены парсером, hint сужает
    квалифицированные вызовы)."""
    out: dict[str, list[dict]] = {n: [] for n in names}
    if not names:
        return out
    srcs, err = ws.resolve_sources(source)
    if err:
        return out
    hints = {k.lower(): (v or "").lower() for k, v in (hints or {}).items()}
    # Есть индекс символов -> отвечаем SQL-выборкой: без текстового скана и без обрезки по
    # числу файлов-кандидатов. Хинты применяются В SQL (сгруппированно по значению), а не
    # игнорируются: иначе review_set приписывал рутине чужих одноимённых вызывающих.
    indexed = _index_callers_grouped(
        ws, names, max_per_name=max_per_name, source=source,
        hints={n: hints.get(n.lower(), "") for n in names},  # ключи — как переданные имена
    )
    if indexed is not None:
        return indexed
    wanted = {n.lower(): n for n in names}
    alternation = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    files, _trunc = _candidate_files(ws, rf"\b(?:{alternation})\s*\(", srcs, None)
    for path in files:
        routines = routines_of(path)
        src_name, rel = ws.source_of_path(path)
        src = next((s for s in ws.sources if s.name == src_name), None)
        descr = describe_bsl_path(src, rel) if src else {"module": "?", "object": "?"}
        obj_low = descr["object"].lower()
        local_names = {r.name.lower() for r in routines}
        for rt in routines:
            rt_low = rt.name.lower()
            for call in rt.calls:
                target = wanted.get(call.method.lower())
                if target is None:
                    continue
                low = target.lower()
                if call.qualifier is None and rt_low == low:
                    continue  # только неквалифицированный самовызов, см. callers_of
                rows = out[target]
                if len(rows) >= max_per_name:
                    continue
                hint = hints.get(low, "")
                if hint:
                    q = (call.qualifier or "").lower()
                    if not (q == hint or (call.qualifier is None and hint in obj_low)):
                        continue
                rows.append({
                    "source": src_name,
                    "path": rel,
                    **{k: v for k, v in descr.items() if k in ("object", "module")},
                    "routine": rt.name,
                    "routine_lines": [rt.start_line, rt.end_line],
                    "qualifier": call.qualifier,
                    "call_line": call.line or None,  # точная строка вызова (не только рутина)
                    "local_target": call.qualifier is None and low in local_names,
                })
    return out


def find_declarations(
    ws: Workspace, routine_name: str, *, exported_only: bool = False, max_results: int = 50,
    source: str = "", decl_offset: int = 0, substring: bool = False,
) -> dict:
    """Where a procedure/function with this exact name is declared (all sources).

    При готовом индексе символов счёт объявлений ПОЛНЫЙ (declaration_count), а не «сколько
    успели найти до обрезки по файлам-кандидатам»: у популярных имён вроде ПриСозданииНаСервере
    объявлений тысячи, и произвольная выборка вводила в заблуждение."""
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    try:
        from . import fts as _fts
        indexed = _fts.index_for(ws).declarations(
            routine_name, exported_only=exported_only, substring=substring)
    except Exception:  # noqa: BLE001
        indexed = None
    if indexed is not None:
        keep = {s.name for s in srcs}
        indexed = [r for r in indexed if r.get("source") in keep]
        # Индекс — не истина: строки по изменённым/удалённым файлам выбрасываем и
        # доразбираем эти файлы живьём (иначе агент получает координаты чужого кода, а
        # только что добавленная рутина «не существует»).
        indexed = _merge_live_declarations(ws, indexed, routine_name, exported_only, srcs)
        # ПУСТОЙ ответ не авторитетен, только если индекс имени не знает (могли добавить после
        # сборки). Если знает, а объявлений нет после фильтров — это корректный ноль, и полный
        # скан ради него не нужен.
        if not indexed and not _index_knows(ws, routine_name):
            indexed = None
    if indexed is not None:
        offset = max(0, decl_offset)
        window = indexed[offset: offset + max(1, max_results)]
        return {
            "routine": routine_name,
            "declaration_count": len(indexed),
            "returned": len(window),
            "offset": offset,
            "truncated": offset + len(window) < len(indexed),
            "engine": "index",
            "declarations": window,
        }
    pattern = (rf"^[ \t]*{_KW}[ \t]+\w*{re.escape(routine_name)}\w*[ \t]*\(" if substring
               else rf"^[ \t]*{_KW}[ \t]+{re.escape(routine_name)}[ \t]*\(")
    files, truncated = _candidate_files(ws, pattern, srcs, None, cap=max_results * 4)
    rows: list[dict] = []
    for path in files:
        for rt in routines_of(path):
            low_rt, low_q = rt.name.lower(), routine_name.lower()
            if (low_q not in low_rt) if substring else (low_rt != low_q):
                continue
            if exported_only and not rt.export:
                continue
            src_name, rel = ws.source_of_path(path)
            src = next((s for s in ws.sources if s.name == src_name), None)
            descr = describe_bsl_path(src, rel) if src else {}
            rows.append({
                "source": src_name, "path": rel, **descr, **routine_row(path, rt),
            })
            if len(rows) >= max_results:
                return _decl_answer(routine_name, rows, truncated=True)
    return _decl_answer(routine_name, rows, truncated=truncated)


def _decl_answer(routine_name: str, rows: list[dict], *, truncated: bool) -> dict:
    """Ответ скан-пути в ТОЙ ЖЕ форме, что и индексного (declaration_count/returned/engine).

    Раньше формы расходились, и вызывающий, получив фолбэк, не находил ожидаемых полей —
    а по `engine` нельзя было понять, полон ли ответ.

    ПОЛНЫЙ счёт скан-путь знает только когда обхода хватило: кандидаты обрезаются по
    `max_results * 4` файлам, а строки — по `max_results`. Пока здесь стояло `len(rows)`,
    обрезанное окно выдавалось за полный счёт: на УТ с пустым индексом `ПередЗаписью` давал
    `declaration_count: 2` против 1401 на индексном пути, и агент делал вывод, что обработчик в
    этой конфигурации почти не используется. Честный ответ при обрезке — `null` (как
    `call_rows_total` у find_callers), а не правдоподобное число."""
    return {
        "routine": routine_name,
        "declaration_count": None if truncated else len(rows),
        "returned": len(rows),
        "offset": 0,
        "truncated": truncated,
        "engine": "scan",
        "declarations": rows,
    }


def call_graph(
    ws: Workspace, routine_name: str, *, depth: int = 2, max_per_level: int = 40,
    source: str = "",
) -> dict:
    """Upward call graph: who (recursively) calls the routine. Parser-verified BFS."""
    depth = max(1, min(depth, 4))
    levels: list[list[dict]] = []
    current: dict[str, str] = {routine_name.lower(): routine_name}  # lower -> display
    visited: set[str] = set()
    for _ in range(depth):
        level_rows: list[dict] = []
        next_names: dict[str, str] = {}
        # Один текстовый скан на весь уровень (было: по одному на каждое имя — на большой
        # конфигурации это десятки секунд на глубину 2+).
        todo = [display for low, display in sorted(current.items()) if low not in visited]
        visited.update(d.lower() for d in todo)
        batch = find_callers_batch(ws, todo, max_per_name=max_per_level, source=source)
        # Настоящий счёт мест вызова уровня берём из индекса, а НЕ из выдачи: выдача уже
        # ограничена max_per_name, поэтому счёт по ней всегда равнялся бы лимиту и «обрезано»
        # никогда бы не выставилось — то есть флаг врал бы так же, как раньше врал routine_count.
        level_rows_total = _index_level_total(ws, todo, source=source)
        level_found = 0  # сколько вызывающих попало в выдачу (после дедупликации по visited)
        for display in todo:
            for row in batch.get(display, []):
                key = f"{row['path']}::{row['routine']}"
                if key in visited:
                    continue
                visited.add(key)
                level_found += 1
                next_names.setdefault(row["routine"].lower(), row["routine"])
                if len(level_rows) >= max_per_level:
                    continue  # считаем дальше, но в выдачу не кладём
                level_rows.append({"calls": display, **row})
        if not level_rows:
            break
        # Счёт по уровню обязателен: раньше обрезка по max_per_level читалась как
        # «вызывающих больше нет», и агент делал вывод о безопасности правки по усечённому графу.
        levels.append({
            "level": len(levels) + 1,
            # из индекса — сколько мест вызова у имён этого уровня всего (None без индекса)
            "level_call_rows_total": level_rows_total,
            "level_returned": len(level_rows),
            "level_truncated": (level_rows_total > len(level_rows)
                                if level_rows_total is not None
                                else level_found > len(level_rows)),
            "callers": level_rows,
        })
        current = next_names
    return {"routine": routine_name, "depth": len(levels), "levels": levels}


# --------------------------------------------------------------------------- #
# Extension overrides
# --------------------------------------------------------------------------- #

_OVERRIDE_RX = r"^\s*&(Вместо|Перед|После|ИзменениеИКонтроль|Around|Before|After|ChangeAndValidate)\b"


_OVERRIDE_INDEX_TTL = 60.0  # с: полный скан переопределений дорог, а меняется он редко
_OVERRIDE_INDEX: dict[str, tuple[float, list[dict]]] = {}


def override_index(ws: Workspace, source: str = "") -> list[dict]:
    """ВСЕ переопределения расширений — полный детерминированный список, TTL-кэш.

    Раньше каждый вызов делал скан с обрезкой по 300 файлов в порядке потока rg, из-за чего
    один и тот же запрос давал разное число строк (663/798/802), а review_set строил из этого
    `overridden_by` — то есть переопределения изменённых рутин могли молча пропасть. Здесь
    обход полный и отсортированный, поэтому ответ воспроизводим; TTL прячет его стоимость от
    подряд идущих вызовов (find_overrides + review_set)."""
    key = f"{str(ws.root).lower()}|{source.lower()}"
    now = time.monotonic()
    hit = _OVERRIDE_INDEX.get(key)
    if hit and now - hit[0] < _OVERRIDE_INDEX_TTL:
        return hit[1]
    # Индекс символов уже содержит override_mode/override_target — берём оттуда, вместо
    # полного текстового скана расширений с повторным разбором модулей.
    try:
        from . import fts as _fts
        indexed = _fts.index_for(ws).overrides()
    except Exception:  # noqa: BLE001 — падение индекса не должно ломать ответ
        indexed = None
    if indexed is not None:
        if source:
            keep = {s.name for s in ws.resolve_sources(source)[0]}
            indexed = [r for r in indexed if r.get("source") in keep]
        else:
            ext = {s.name for s in ws.sources if s.is_extension}
            indexed = [r for r in indexed if r.get("source") in ext]
        indexed = _merge_live_overrides(ws, indexed)
        _OVERRIDE_INDEX[key] = (now, indexed)
        return indexed
    if source:
        srcs, err = ws.resolve_sources(source)
        if err:
            return []
    else:
        srcs = [s for s in ws.sources if s.is_extension]
    rows: list[dict] = []
    if srcs:
        files, _trunc = _candidate_files(ws, _OVERRIDE_RX, srcs, None, complete=True)
        for path in files:
            src_name, rel = ws.source_of_path(path)
            src = next((s for s in ws.sources if s.name == src_name), None)
            descr = describe_bsl_path(src, rel) if src else {}
            for rt in routines_of(path):
                if not rt.override_mode:
                    continue
                rows.append({
                    "source": src_name,
                    "object": descr.get("object"),
                    "module": descr.get("module"),
                    "path": rel,
                    "routine": rt.name,
                    "mode": rt.override_mode,
                    "target": rt.override_target,
                    "directive": rt.directive,
                    "lines": [rt.start_line, rt.end_line],
                })
    _OVERRIDE_INDEX[key] = (now, rows)
    return rows


def find_overrides(
    ws: Workspace, *, kind: str = "", name: str = "", method: str = "", source: str = "",
    max_results: int = 100, offset: int = 0,
) -> dict:
    """Extension override hooks (&Вместо/&Перед/&После/&ИзменениеИКонтроль) with targets.

    Filters: kind+name (заимствованный объект), method (базовый метод), source (расширение).
    Ответ детерминирован и полон по счёту: `override_count` — всего найдено, отдаётся окно
    `offset`..`offset+max_results` (докрутить — увеличить offset), поэтому «обрезано» больше
    не означает «неизвестно сколько».
    """
    if source:
        _srcs, err = ws.resolve_sources(source)
        if err:
            return {"error": err}
    elif not any(s.is_extension for s in ws.sources):
        return {"override_count": 0, "overrides": [], "note": "В рабочей копии нет расширений."}
    want_obj = f"{kind}.{name}".lower() if kind and name else ""
    want_kind = f"{kind}.".lower() if kind and not name else ""
    method_low = method.lower()
    rows = [
        r for r in override_index(ws, source)
        if (not want_obj or (r.get("object") or "").lower() == want_obj)
        and (not want_kind or (r.get("object") or "").lower().startswith(want_kind))
        and (not method_low or method_low in ((r.get("target") or "").lower(),
                                             (r.get("routine") or "").lower()))
    ]
    offset = max(0, offset)
    window = rows[offset: offset + max(1, max_results)]
    return {
        "override_count": len(rows),
        "returned": len(window),
        "offset": offset,
        "truncated": offset + len(window) < len(rows),
        "overrides": window,
    }


# --------------------------------------------------------------------------- #
# Handlers / entry points
# --------------------------------------------------------------------------- #

def find_handlers(ws: Workspace, kind: str, name: str, source: str = "") -> dict:
    """Form event wiring + module entry points of one object (проведение/запись/...).

    Adopted objects are merged across sources (extension-first): the extension's copy of a
    same-named form shadows the base one, base-only forms and modules are still included —
    the platform-view of the object, not one file tree."""
    cands, err = ws.find_objects(kind, name, source)
    if err:
        return {"error": err}
    form_rows: list[dict] = []
    module_rows: list[dict] = []
    seen_forms: set[str] = set()
    for src, ref in cands:
        obj = ws.parse_object(src, ref)
        for form in obj.forms:
            if form.name.lower() in seen_forms or not form.form_path:
                continue
            seen_forms.add(form.name.lower())
            declared: dict[str, Routine] = {}
            if form.module_path:
                declared = {r.name.lower(): r for r in routines_of(Path(form.module_path))}
            for h in parse_form_handlers(form.form_path):
                rt = declared.get((h.get("handler") or "").lower())
                form_rows.append({
                    "source": src.name,
                    "form": form.name,
                    "event": h.get("event"),
                    "element": h.get("element"),
                    "handler": h.get("handler"),
                    "declared": rt is not None,
                    "lines": [rt.start_line, rt.end_line] if rt else None,
                })
        for mod in obj.modules:
            mpath = Path(mod.path)
            for rt in routines_of(mpath):
                ep = classify_entry_point(rt.name)
                if ep:
                    module_rows.append({
                        "source": src.name,
                        "module": mod.module_type,
                        "routine": rt.name,
                        "entry_point": ep,
                        "export": rt.export,
                        "lines": [rt.start_line, rt.end_line],
                    })
    return {
        "sources": [s.name for s, _ in cands],
        "object": f"{kind}.{name}",
        "form_handlers": form_rows,
        "module_entry_points": module_rows,
        "subscriptions": subscriptions_for(ws, kind, name, source),
    }


# --------------------------------------------------------------------------- #
# Register movements
# --------------------------------------------------------------------------- #

def writes_to(ws: Workspace, *, document: str = "", register: str = "", source: str = "") -> dict:
    """Документ -> регистры (RegisterRecords) и обратный вопрос «кто пишет в регистр».

    Обратный поиск разбирает метаданные всех документов источника (кэшируется).
    """
    if document:
        kind, _, name = document.partition(".")
        if not name:
            kind, name = "Document", document
        cands, err = ws.find_objects(kind, name, source)
        if err:
            return {"error": err}
        # Движения заимствованного документа расширение ДОПОЛНЯЕТ, а его копия .mdo при этом
        # часто вообще не содержит RegisterRecords. Брать первый источник (расширения раньше
        # базы) означало отвечать «регистров нет» при непустом списке — проверено на боевом
        # документе: 0 вместо 4 (1 в базе + 3 добавлены расширением). Объединяем по всем копиям.
        registers: list[str] = []
        per_source: dict[str, list[str]] = {}
        fqn = f"{kind}.{name}"
        for cand_src, cand_ref in cands:
            try:
                obj = ws.parse_object(cand_src, cand_ref)
            except ValueError:
                continue
            fqn = obj.fqn
            own = list(obj.register_records)
            if own:
                per_source[cand_src.name] = own
            registers += [r for r in own if r not in registers]
        return {"source": (next(iter(per_source), cands[0][0].name) if cands else ""),
                "document": fqn,
                "sources_checked": [s.name for s, _ in cands],
                "registers": registers,
                **({"registers_by_source": per_source} if len(per_source) > 1 else {})}
    if not register:
        return {"error": "Укажите document или register."}
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    reg_low = register.lower()
    writers: list[dict] = []
    for s in srcs:
        for fqn, meta, obj_dir in ws.listing(s):
            if not fqn.startswith("Document."):
                continue
            try:
                obj = ws.parse_object(s, (fqn, meta, obj_dir))
            except ValueError:
                continue
            for reg in obj.register_records:
                if reg.lower() == reg_low or reg.lower().endswith("." + reg_low):
                    writers.append({"source": s.name, "document": fqn, "register": reg})
                    break
    return {"register": register, "writer_count": len(writers), "writers": writers}


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def metrics(ws: Workspace, source: str = "") -> dict:
    """Inventory of the working copy: objects by kind, module files/bytes, routine counts."""
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    per_source: list[dict] = []
    for s in srcs:
        files = ws.bsl_files(s)
        code_bytes = 0
        for p in files:
            try:
                code_bytes += p.stat().st_size
            except OSError:
                pass
        decl = rf"^[ \t]*{_KW}[ \t]+[\wА-Яа-яЁё]+[ \t]*\("
        routines = search.count_total(decl, [str(s.files_root / f) for f in sorted(CODE_FOLDERS)
                                             if (s.files_root / f).is_dir()])
        row = {
            "source": s.name,
            "format": s.fmt,
            "is_extension": s.is_extension,
            "objects_by_kind": ws.kind_counts(s),
            "bsl_files": len(files),
            "code_bytes": code_bytes,
            "routines": routines,
        }
        if s.is_extension:
            row["override_annotations"] = search.count_total(
                _OVERRIDE_RX, [str(s.files_root / f) for f in sorted(CODE_FOLDERS)
                               if (s.files_root / f).is_dir()])
        per_source.append(row)
    # Состояние индекса — часть инвентаря, а не деталь реализации: без него оператор не видит,
    # что воркспейс сидит на пустом индексе и все ответы идут медленным сканом с урезанными
    # счётчиками. Именно так два воркспейса молча простояли с БД на гигабайты и нулём рутин.
    try:
        from . import fts as _fts
        index = _fts.index_for(ws).status()
    except Exception as exc:  # noqa: BLE001
        index = {"error": f"состояние индекса недоступно: {exc}"}
    return {"sources": per_source,
            "index": index,
            "note": None if search.rg_path() else "ripgrep не найден: routines не посчитаны"}
