"""Build the BSL call graph: parse module files -> routines -> resolved CALLS edges.

Supports full and incremental rebuilds. Incremental reprocesses only objects whose
configVersion changed. A changed CommonModule has incoming CALLS from unchanged callers,
so if any changed object is a CommonModule we fall back to a full rebuild (correctness).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .bsl.parser import parse_module
from .chunking import classify_entry_point
from .config import Settings
from .parsing.forms import parse_form_handlers
from .progress import Progress
from .storage import Neo4jStore

log = logging.getLogger(__name__)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _parse_modules(modules: list[dict],
                   progress_label: str | None = None) -> tuple[list[dict], list[tuple], dict, dict, dict, dict]:
    """Parse module files -> (routine_rows, parsed, local_index, common_index, manager_index, stats)."""
    routine_rows: list[dict] = []
    local_index: dict[str, dict[str, str]] = {}
    common_index: dict[str, dict[str, str]] = {}
    manager_index: dict[str, dict[str, str]] = {}  # object name -> {method: fqn} for ManagerModule
    parsed: list[tuple[str, list]] = []
    modules_parsed = files_missing = total_routines = 0
    prog = Progress(len(modules), progress_label, unit="модулей", rate_word="модуль/с") if progress_label else None

    for m in modules:
        if prog:
            prog.advance()
        text = _read(m["path"])
        if text is None:
            files_missing += 1
            continue
        routines = parse_module(text)
        modules_parsed += 1
        total_routines += len(routines)
        mf = m["module_fqn"]
        local = local_index.setdefault(mf, {})
        for rt in routines:
            rfqn = f"{mf}::{rt.name}"
            local[rt.name] = rfqn
            routine_rows.append({
                "fqn": rfqn, "module_fqn": mf,
                "props": {
                    "name": rt.name, "routine_kind": rt.kind, "export": rt.export,
                    "region": rt.region, "object_fqn": m["obj_fqn"], "object_kind": m["obj_kind"],
                    "module_type": m["mtype"], "config_version": m.get("config_version"),
                    "entry_point": classify_entry_point(rt.name),
                },
            })
        if m["obj_kind"] == "CommonModule":
            ci = common_index.setdefault(m["obj_name"], {})
            for rt in routines:
                ci[rt.name] = f"{mf}::{rt.name}"
        elif m["mtype"] == "ManagerModule":
            # Resolves Справочники.X.Метод() / Документы.X.Метод() etc. The regex captures the
            # object name as qualifier (collection prefix is dropped), so we key by object name.
            mi = manager_index.setdefault(m["obj_name"], {})
            for rt in routines:
                mi[rt.name] = f"{mf}::{rt.name}"
        parsed.append((mf, routines))

    if prog:
        prog.finish()
    stats = {"modules_parsed": modules_parsed, "files_missing": files_missing,
             "routines": total_routines}
    return routine_rows, parsed, local_index, common_index, manager_index, stats


def _parse_form_modules(forms: list[dict],
                        progress_label: str | None = None) -> tuple[list[dict], list[tuple], dict, list[dict], dict]:
    """Parse form modules -> (routine_rows, parsed, local_index, handles_rows, stats)."""
    routine_rows: list[dict] = []
    local_index: dict[str, dict[str, str]] = {}
    parsed: list[tuple[str, list]] = []
    handles_rows: list[dict] = []
    parsed_cnt = missing = total = 0
    prog = Progress(len(forms), progress_label, unit="форм", rate_word="форма/с") if progress_label else None

    for f in forms:
        if prog:
            prog.advance()
        text = _read(f["path"])
        if text is None:
            missing += 1
            continue
        routines = parse_module(text)
        parsed_cnt += 1
        total += len(routines)
        ff = f["form_fqn"]
        local = local_index.setdefault(ff, {})
        # Map handler name -> event up front so a form routine wired to a UI event is tagged
        # as a 'событие_формы' entry point on its Routine node.
        handlers = {h["handler"]: h for h in parse_form_handlers(f["form_path"])} if f.get("form_path") else {}
        for rt in routines:
            rfqn = f"{ff}::{rt.name}"
            local[rt.name] = rfqn
            ev = handlers.get(rt.name)
            routine_rows.append({
                "fqn": rfqn, "form_fqn": ff,
                "props": {
                    "name": rt.name, "routine_kind": rt.kind, "export": rt.export,
                    "region": rt.region, "directive": rt.directive, "object_fqn": f["owner_fqn"],
                    "object_kind": f["owner_kind"], "module_type": "FormModule",
                    "entry_point": classify_entry_point(rt.name, form_event=ev["event"] if ev else None),
                },
            })
        parsed.append((ff, routines))
        for h in handlers.values():
            target = local.get(h["handler"])
            if target:
                handles_rows.append({"form_fqn": ff, "routine_fqn": target,
                                     "event": h["event"], "element": h.get("element")})

    if prog:
        prog.finish()
    stats = {"form_modules_parsed": parsed_cnt, "form_routines": total, "form_files_missing": missing}
    return routine_rows, parsed, local_index, handles_rows, stats


def _resolve(parsed: list[tuple], local_index: dict, common_index: dict,
             manager_index: dict | None = None) -> tuple[list[dict], dict]:
    manager_index = manager_index or {}
    call_rows: list[dict] = []
    res_local = res_common = res_manager = unresolved = 0
    for mf, routines in parsed:
        local = local_index.get(mf, {})
        for rt in routines:
            src = f"{mf}::{rt.name}"
            for call in rt.calls:
                if call.qualifier is None:
                    dst = local.get(call.method)
                    if dst:
                        call_rows.append({"src": src, "dst": dst, "confidence": "high", "kind": "local"})
                        res_local += 1
                    else:
                        unresolved += 1
                else:
                    ci = common_index.get(call.qualifier)
                    mi = manager_index.get(call.qualifier)
                    if ci and call.method in ci:
                        call_rows.append({"src": src, "dst": ci[call.method],
                                          "confidence": "high", "kind": "common_module"})
                        res_common += 1
                    elif mi and call.method in mi:
                        # Справочники.X.Метод() / Документы.X.Метод() -> manager module method.
                        call_rows.append({"src": src, "dst": mi[call.method],
                                          "confidence": "medium", "kind": "manager"})
                        res_manager += 1
                    else:
                        unresolved += 1
    stats = {"calls_resolved_local": res_local, "calls_resolved_common_module": res_common,
             "calls_resolved_manager": res_manager, "calls_unresolved": unresolved}
    return call_rows, stats


def _full(tenant_id: str, store: Neo4jStore, reason: str | None = None) -> dict[str, Any]:
    modules = store.routine_modules(tenant_id)
    routine_rows, parsed, local_index, common_index, manager_index, pstats = _parse_modules(
        modules, progress_label=f"callgraph:{tenant_id} модули")
    f_rows, f_parsed, f_local, handles_rows, fstats = _parse_form_modules(
        store.form_modules(tenant_id), progress_label=f"callgraph:{tenant_id} формы")
    local_index.update(f_local)

    store.delete_routines(tenant_id)
    store.write_routines(tenant_id, routine_rows)
    store.write_form_routines(tenant_id, f_rows)
    call_rows, rstats = _resolve(parsed + f_parsed, local_index, common_index, manager_index)
    written = store.write_calls(tenant_id, call_rows)
    handles = store.write_handles(tenant_id, handles_rows)
    return {"mode": "full", "tenant_id": tenant_id, "fallback_reason": reason,
            **pstats, **fstats, **rstats, "calls_written": written, "handles_written": handles}


def build_call_graph(
    tenant_id: str, settings: Settings, reset: bool = True, incremental: bool = False
) -> dict[str, Any]:
    with Neo4jStore.from_settings(settings) as store:
        store.ensure_schema()

        if not incremental:
            return _full(tenant_id, store)

        stale = store.stale_routine_owners(tenant_id)
        if not stale:
            return {"mode": "incremental", "tenant_id": tenant_id, "stale_objects": 0,
                    "routines": 0, "calls_written": 0}
        if any(kind == "CommonModule" for _, kind in stale):
            # Changed common module -> unchanged callers' incoming CALLS must be re-resolved.
            return _full(tenant_id, store, reason="changed CommonModule(s)")

        stale_fqns = [fqn for fqn, _ in stale]
        modules = store.routine_modules(tenant_id, only=stale_fqns)
        routine_rows, parsed, local_index, _common, _manager, pstats = _parse_modules(
            modules, progress_label=f"callgraph:{tenant_id} модули (incr)")
        common_index = store.common_module_routine_index(tenant_id)  # from graph (unchanged)
        manager_index = store.manager_module_routine_index(tenant_id)  # from graph (unchanged)
        store.delete_routines_for(tenant_id, stale_fqns)
        store.write_routines(tenant_id, routine_rows)
        call_rows, rstats = _resolve(parsed, local_index, common_index, manager_index)
        written = store.write_calls(tenant_id, call_rows)
        return {"mode": "incremental", "tenant_id": tenant_id, "stale_objects": len(stale),
                **pstats, **rstats, "calls_written": written}
