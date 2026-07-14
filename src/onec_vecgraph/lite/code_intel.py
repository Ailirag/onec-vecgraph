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
from ..parsing.dump import TYPE_FOLDERS
from ..parsing.forms import parse_form_handlers
from . import search
from .workspace import LiteSource, Workspace, read_text

_KW = r"(?:Процедура|Функция|Procedure|Function)"

# Parsed-module cache: path -> (mtime, routines). Safe across workspaces (path is absolute).
_MODULES: dict[str, tuple[float, list[Routine]]] = {}
# Per-source "name -> [(kind, fqn)]" index for qualifier resolution; TTL-refreshed.
_NAME_INDEX: dict[str, tuple[float, dict[str, list[tuple[str, str]]]]] = {}
_NAME_TTL = 30.0

_MAX_CANDIDATE_FILES = 300  # cap on files re-parsed per callers/overrides query


def clear_caches() -> None:
    """Drop parsed-module and name-index caches (workspace re-point / admin refresh)."""
    _MODULES.clear()
    _NAME_INDEX.clear()


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
    _MODULES[key] = (mtime, routines)
    return routines


def _signature(path: Path, rt: Routine) -> str:
    """Declaration line(s) of a routine up to the closing paren (max 5 lines)."""
    try:
        lines = read_text(path).splitlines()
    except OSError:
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
    kind = TYPE_FOLDERS.get(parts[0], parts[0])
    name = parts[1] if len(parts) > 1 else ""
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
) -> dict:
    """Где тип объекта упоминается в МЕТАДАННЫХ: реквизиты объектов и форм, подписки,
    определяемые типы. Матчит `<Вид>Ref.<Имя>` и `<Вид>Object.<Имя>` в .xml/.mdo/.form."""
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    pattern = rf"{kind}(?:Ref|Object)\.{re.escape(name)}\b"
    rows: list[dict] = []
    truncated = False
    for s in srcs:
        globs = ("*.mdo", "*.form") if s.fmt == "edt" else ("*.xml",)
        for glob in globs:
            _engine, it = search.stream(
                ws, pattern, sources=[s], kinds=None, glob=glob, regex=True,
                max_hits=max_results + 1,
            )
            for abs_path, line_no, text in it:
                src_name, rel = ws.source_of_path(Path(abs_path))
                owner = _meta_owner(rel)
                if owner is None:
                    continue
                if len(rows) >= max_results:
                    truncated = True
                    break
                rows.append({
                    "source": src_name,
                    "object": owner,
                    "artifact": artifact_of(rel),
                    "path": rel,
                    "line": line_no,
                    "text": text.strip()[:200],
                })
            if truncated:
                break
        if truncated:
            break
    return {"type": f"{kind}.{name}", "match_count": len(rows), "truncated": truncated,
            "usages": rows}


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

    usages = type_usages(ws, kind, name, max_results=500, source=source)
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

def _name_index(ws: Workspace, src: LiteSource) -> dict[str, list[tuple[str, str]]]:
    now = time.monotonic()
    hit = _NAME_INDEX.get(src.name)
    if hit and now - hit[0] < _NAME_TTL:
        return hit[1]
    idx: dict[str, list[tuple[str, str]]] = {}
    for fqn, _meta, _dir in ws.listing(src):
        kind, _, tail = fqn.partition(".")
        idx.setdefault(tail.lower(), []).append((kind, fqn))
    _NAME_INDEX[src.name] = (now, idx)
    return idx


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
        candidates = _name_index(ws, src).get(qualifier.lower(), [])
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
        calls = [_resolve_call(ws, c.qualifier, c.method, local, (src, mpath)) for c in rt.calls]
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
    cap: int = _MAX_CANDIDATE_FILES,
) -> tuple[list[Path], bool]:
    """Distinct files with a text-level match, in stream order; (files, truncated)."""
    _engine, it = search.stream(
        ws, pattern, sources=sources, kinds=kinds, regex=True, max_hits=cap * 40,
    )
    files: list[Path] = []
    seen: set[str] = set()
    truncated = False
    for abs_path, _line, _text in it:
        if abs_path in seen:
            continue
        seen.add(abs_path)
        files.append(Path(abs_path))
        if len(files) >= cap:
            truncated = True
            break
    return files, truncated


def _enclosing(routines: list[Routine], line: int) -> Routine | None:
    for rt in routines:
        if rt.start_line <= line <= rt.end_line:
            return rt
    return None


def find_callers(
    ws: Workspace, routine_name: str, *, object_hint: str = "", kinds: list[str] | None = None,
    max_results: int = 100, source: str = "",
) -> dict:
    """Call sites of a routine by name: parser-verified, declarations excluded.

    object_hint narrows qualified calls to `Хинт.Метод(...)` (плюс неквалифицированные
    вызовы внутри модулей самого объекта/модуля Хинт).
    """
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    kindset = set(kinds) if kinds else None
    pattern = rf"\b{re.escape(routine_name)}\s*\("
    files, files_truncated = _candidate_files(ws, pattern, srcs, kindset)
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
            if rt.name.lower() == routine_name.lower():
                continue  # its own declaration/body is not a call site
            for call in rt.calls:
                if call.method.lower() != routine_name.lower():
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
    return {
        "routine": routine_name,
        "match_count": len(rows),
        "truncated": truncated or files_truncated,
        "callers": rows,
    }


def find_declarations(
    ws: Workspace, routine_name: str, *, exported_only: bool = False, max_results: int = 50,
    source: str = "",
) -> dict:
    """Where a procedure/function with this exact name is declared (all sources)."""
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    pattern = rf"^[ \t]*{_KW}[ \t]+{re.escape(routine_name)}[ \t]*\("
    files, truncated = _candidate_files(ws, pattern, srcs, None, cap=max_results * 4)
    rows: list[dict] = []
    for path in files:
        for rt in routines_of(path):
            if rt.name.lower() != routine_name.lower():
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
                return {"routine": routine_name, "declarations": rows, "truncated": True}
    return {"routine": routine_name, "declarations": rows, "truncated": truncated}


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
        for low, display in sorted(current.items()):
            if low in visited:
                continue
            visited.add(low)
            res = find_callers(ws, display, max_results=max_per_level, source=source)
            for row in res.get("callers", []):
                key = f"{row['path']}::{row['routine']}"
                if key in visited:
                    continue
                visited.add(key)
                row = {"calls": display, **row}
                level_rows.append(row)
                next_names.setdefault(row["routine"].lower(), row["routine"])
                if len(level_rows) >= max_per_level:
                    break
            if len(level_rows) >= max_per_level:
                break
        if not level_rows:
            break
        levels.append(level_rows)
        current = next_names
    return {"routine": routine_name, "depth": len(levels), "levels": levels}


# --------------------------------------------------------------------------- #
# Extension overrides
# --------------------------------------------------------------------------- #

_OVERRIDE_RX = r"^\s*&(Вместо|Перед|После|ИзменениеИКонтроль|Around|Before|After|ChangeAndValidate)\b"


def find_overrides(
    ws: Workspace, *, kind: str = "", name: str = "", method: str = "", source: str = "",
) -> dict:
    """Extension override hooks (&Вместо/&Перед/&После/&ИзменениеИКонтроль) with targets.

    Filters: kind+name (заимствованный объект), method (базовый метод), source (расширение).
    """
    if source:
        srcs, err = ws.resolve_sources(source)
        if err:
            return {"error": err}
    else:
        srcs = [s for s in ws.sources if s.is_extension]
        if not srcs:
            return {"overrides": [], "note": "В рабочей копии нет расширений."}
    files, truncated = _candidate_files(ws, _OVERRIDE_RX, srcs, {kind} if kind else None)
    rows: list[dict] = []
    want_obj = f"{kind}.{name}".lower() if kind and name else ""
    for path in files:
        src_name, rel = ws.source_of_path(path)
        src = next((s for s in ws.sources if s.name == src_name), None)
        descr = describe_bsl_path(src, rel) if src else {}
        if want_obj and descr.get("object", "").lower() != want_obj:
            continue
        for rt in routines_of(path):
            if not rt.override_mode:
                continue
            target = rt.override_target or rt.name
            if method and method.lower() not in (target.lower(), rt.name.lower()):
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
    return {"override_count": len(rows), "truncated": truncated, "overrides": rows}


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
        src, ref, also, err = ws.find_object(kind, name, source)
        if err:
            return {"error": err}
        assert src is not None and ref is not None
        obj = ws.parse_object(src, ref)
        return {"source": src.name, "document": obj.fqn, "also_in": also,
                "registers": obj.register_records}
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
        routines = search.count_total(decl, [str(s.files_root / f) for f in sorted(TYPE_FOLDERS)
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
                _OVERRIDE_RX, [str(s.files_root / f) for f in sorted(TYPE_FOLDERS)
                               if (s.files_root / f).is_dir()])
        per_source.append(row)
    return {"sources": per_source,
            "note": None if search.rg_path() else "ripgrep не найден: routines не посчитаны"}
