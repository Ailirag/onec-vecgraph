"""Read queries over the metadata graph (tenant-scoped). Used by MCP tools and CLI."""

from __future__ import annotations

from typing import Any

from .storage import Neo4jStore

# Field-membership path: Object -> Field, or Object -> TabularSection -> Field.
_FIELD_PATH = "[:HAS_ATTRIBUTE|HAS_DIMENSION|HAS_RESOURCE|HAS_TABULAR_SECTION*1..2]"
_RELATED_RELS = "OWNED_BY|CONTAINS|SUBSCRIBES|HANDLED_BY|HAS_SUBSYSTEM|HAS_RIGHT_ON|WRITES_TO"


def list_metadata(
    store: Neo4jStore,
    tenant_id: str,
    kind: str | None = None,
    name_contains: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    return store.read(
        "MATCH (o:Object {tenant_id: $t}) "
        "WHERE ($kind IS NULL OR o.kind = $kind) "
        "  AND ($needle IS NULL OR toLower(o.name) CONTAINS toLower($needle) "
        "       OR toLower(coalesce(o.synonym,'')) CONTAINS toLower($needle)) "
        "  AND coalesce(o.stub, false) = false "
        "RETURN o.fqn AS fqn, o.kind AS kind, o.name AS name, o.synonym AS synonym, "
        "       o.config_id AS config_id "
        "ORDER BY o.kind, o.name LIMIT $limit",
        t=tenant_id, kind=kind, needle=name_contains, limit=limit,
    )


def _resolve(store: Neo4jStore, tenant_id: str, q: str) -> dict[str, Any] | None:
    rows = store.read(
        "MATCH (o:Object {tenant_id: $t}) WHERE o.fqn = $q OR o.name = $q "
        "RETURN o.fqn AS fqn, properties(o) AS props ORDER BY coalesce(o.stub,false) LIMIT 1",
        t=tenant_id, q=q,
    )
    return rows[0] if rows else None


def _object_details(store: Neo4jStore, tenant_id: str, fqn: str) -> dict[str, Any]:
    """Full raw property map from the sidecar :Detail node (empty if not indexed yet)."""
    rows = store.read(
        "MATCH (:Object {tenant_id: $t, fqn: $fqn})-[:HAS_DETAIL]->(d:Detail) "
        "RETURN properties(d) AS p",
        t=tenant_id, fqn=fqn,
    )
    props = dict(rows[0]["p"]) if rows else {}
    props.pop("tenant_id", None)
    props.pop("fqn", None)
    props.pop("config_id", None)
    return props


def get_object_properties(store: Neo4jStore, tenant_id: str, q: str) -> dict[str, Any]:
    """Full metadata property set for an object (every <Properties>: Hierarchical, CodeLength,
    Posting, Periodicity, full-text search, lock mode, standard attributes, ...). For developer/
    analyst deep-dives; these are stored but intentionally not vectorized."""
    head = _resolve(store, tenant_id, q)
    if head is None:
        return {"found": False, "query": q}
    fqn = head["fqn"]
    return {
        "found": True,
        "fqn": fqn,
        "kind": head["props"].get("kind"),
        "name": head["props"].get("name"),
        "properties": _object_details(store, tenant_id, fqn),
    }


def get_object(store: Neo4jStore, tenant_id: str, q: str, detail: bool = False) -> dict[str, Any]:
    head = _resolve(store, tenant_id, q)
    if head is None:
        return {"found": False, "query": q}
    fqn = head["fqn"]

    fields = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[m:HAS_ATTRIBUTE|HAS_DIMENSION|HAS_RESOURCE]->(f:Field) "
        "OPTIONAL MATCH (f)-[:REFERENCES]->(ref:Object) "
        "RETURN f.name AS name, f.role AS role, f.synonym AS synonym, f.type_text AS type, "
        "       collect(DISTINCT ref.fqn) AS references ORDER BY f.role, f.name",
        t=tenant_id, fqn=fqn,
    )
    tabular = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:HAS_TABULAR_SECTION]->(ts:TabularSection) "
        "OPTIONAL MATCH (ts)-[:HAS_ATTRIBUTE]->(f:Field) "
        "RETURN ts.name AS name, ts.synonym AS synonym, "
        "       collect(DISTINCT {name: f.name, type: f.type_text}) AS fields ORDER BY ts.name",
        t=tenant_id, fqn=fqn,
    )
    enum_values = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:HAS_ENUM_VALUE]->(e:EnumValue) "
        "RETURN e.name AS name, e.synonym AS synonym ORDER BY e.name",
        t=tenant_id, fqn=fqn,
    )
    predefined = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:HAS_PREDEFINED]->(p:Predefined) "
        "RETURN p.name AS name, p.code AS code, p.description AS description ORDER BY p.name",
        t=tenant_id, fqn=fqn,
    )
    forms = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:HAS_FORM]->(f:Form) RETURN f.name AS name ORDER BY f.name",
        t=tenant_id, fqn=fqn,
    )
    modules = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:HAS_MODULE]->(m:Module) "
        "RETURN m.module_type AS type, m.size AS size ORDER BY m.module_type",
        t=tenant_id, fqn=fqn,
    )
    owners = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:OWNED_BY]->(x:Object) RETURN x.fqn AS fqn",
        t=tenant_id, fqn=fqn,
    )
    subsystems = store.read(
        "MATCH (s:Object {tenant_id: $t})-[:CONTAINS]->(o:Object {fqn: $fqn}) RETURN s.fqn AS fqn",
        t=tenant_id, fqn=fqn,
    )
    result = {
        "found": True,
        "fqn": fqn,
        "kind": head["props"].get("kind"),
        "name": head["props"].get("name"),
        "synonym": head["props"].get("synonym"),
        "comment": head["props"].get("comment"),
        "config_id": head["props"].get("config_id"),
        "belonging": head["props"].get("belonging"),
        "fields": fields,
        "tabular_sections": tabular,
        "enum_values": enum_values,
        "predefined": predefined,
        "forms": [f["name"] for f in forms],
        "modules": modules,
        "owners": [o["fqn"] for o in owners],
        "in_subsystems": [s["fqn"] for s in subsystems],
    }
    if detail:
        result["details"] = _object_details(store, tenant_id, fqn)
    return result


def find_type_usages(store: Neo4jStore, tenant_id: str, q: str) -> dict[str, Any]:
    head = _resolve(store, tenant_id, q)
    if head is None:
        return {"found": False, "query": q}
    usages = store.read(
        f"MATCH (owner:Object {{tenant_id: $t}})-{_FIELD_PATH}->(f:Field)-[:REFERENCES]->"
        f"(dep:Object {{tenant_id: $t, fqn: $fqn}}) "
        "RETURN DISTINCT owner.fqn AS owner, f.name AS field, f.role AS role, f.type_text AS type "
        "ORDER BY owner, field",
        t=tenant_id, fqn=head["fqn"],
    )
    return {"found": True, "fqn": head["fqn"], "used_by": usages, "count": len(usages)}


# ── Phase-2 overlay union (baseline ∪ per-task overlay) ───────────────────────────────
# Rule: an edge is "live" in the tenant that owns its SOURCE object's current version —
# overlay if the source is touched (present in the overlay tenant), else baseline. Tombstones
# (deletions recorded in the overlay tenant) drop the object and any edge into it. Every merged
# row is tagged `layer` = 'working' (overlay) | 'release' (baseline) for provenance.

def _overlay_sets(store: Neo4jStore, overlay_tenant_id: str) -> tuple[set[str], set[str]]:
    """(touched, tombstoned) fqns for an overlay tenant: touched = objects present there."""
    touched = {
        r["fqn"]
        for r in store.read("MATCH (o:Object {tenant_id: $t}) RETURN o.fqn AS fqn", t=overlay_tenant_id)
    }
    return touched, set(store.tombstoned_fqns(overlay_tenant_id))


def _merge_edge_rows(base: list[dict], overlay: list[dict], *, src_key: str,
                     touched: set[str], tombstoned: set[str]) -> list[dict]:
    """Union edge-rows by the override/tombstone rule. `src_key` names the field holding the
    edge's source-object fqn (the side that owns the relationship). Pure / unit-tested."""
    out: dict[tuple, dict] = {}
    for r in overlay:
        if r.get(src_key) in tombstoned:
            continue
        out[(r.get("rel"), r.get("fqn"))] = {**r, "layer": "working"}
    for r in base:
        s = r.get(src_key)
        if s in touched or s in tombstoned:
            continue  # baseline edge superseded by overlay, or source deleted
        k = (r.get("rel"), r.get("fqn"))
        if k not in out:
            out[k] = {**r, "layer": "release"}
    return sorted(out.values(), key=lambda r: (r.get("rel") or "", r.get("fqn") or ""))


def _tag_layer(rows: list[dict], layer: str, tombstoned: set[str]) -> list[dict]:
    """Tag owned (out-side) rows with a layer and drop those pointing at a tombstoned object."""
    return [{**r, "layer": layer} for r in rows if r.get("fqn") not in tombstoned]


def _deps_single(store: Neo4jStore, tenant_id: str, fqn: str, direction: str) -> dict[str, list]:
    """The four raw dependency lists for one tenant (no merge)."""
    out_refs = out_related = in_refs = in_related = []
    if direction in ("out", "both"):
        out_refs = store.read(
            f"MATCH (o:Object {{tenant_id: $t, fqn: $fqn}})-{_FIELD_PATH}->(:Field)-[:REFERENCES]->(dep:Object) "
            "RETURN DISTINCT dep.fqn AS fqn, dep.kind AS kind, coalesce(dep.stub,false) AS stub "
            "ORDER BY fqn",
            t=tenant_id, fqn=fqn,
        )
        out_related = store.read(
            f"MATCH (o:Object {{tenant_id: $t, fqn: $fqn}})-[r:{_RELATED_RELS}]->(dep:Object) "
            "RETURN type(r) AS rel, dep.fqn AS fqn, dep.kind AS kind ORDER BY rel, fqn",
            t=tenant_id, fqn=fqn,
        )
    else:
        out_refs, out_related = [], []

    if direction in ("in", "both"):
        in_refs = store.read(
            f"MATCH (src:Object {{tenant_id: $t}})-{_FIELD_PATH}->(:Field)-[:REFERENCES]->"
            f"(o:Object {{tenant_id: $t, fqn: $fqn}}) "
            "RETURN DISTINCT src.fqn AS fqn, src.kind AS kind ORDER BY fqn",
            t=tenant_id, fqn=fqn,
        )
        in_related = store.read(
            f"MATCH (src:Object {{tenant_id: $t}})-[r:{_RELATED_RELS}]->(o:Object {{tenant_id: $t, fqn: $fqn}}) "
            "RETURN type(r) AS rel, src.fqn AS fqn, src.kind AS kind ORDER BY rel, fqn",
            t=tenant_id, fqn=fqn,
        )
    else:
        in_refs, in_related = [], []

    return {"out_refs": out_refs, "out_related": out_related,
            "in_refs": in_refs, "in_related": in_related}


def get_dependencies(store: Neo4jStore, tenant_id: str, q: str, direction: str = "out",
                     overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Dependencies of an object. With `overlay_tenant_id`, union baseline ∪ overlay: outgoing
    edges follow the queried object's live version; incoming edges merge by source ownership;
    tombstones mask deletions; rows carry `layer` (release/working)."""
    head = _resolve(store, tenant_id, q) or (
        _resolve(store, overlay_tenant_id, q) if overlay_tenant_id else None)
    if head is None:
        return {"found": False, "query": q}
    fqn = head["fqn"]

    if not overlay_tenant_id:
        base = _deps_single(store, tenant_id, fqn, direction)
        return {
            "found": True, "fqn": fqn, "direction": direction,
            "depends_on": {"references": base["out_refs"], "related": base["out_related"]},
            "dependents": {"referenced_by": base["in_refs"], "related": base["in_related"]},
        }

    touched, tombstoned = _overlay_sets(store, overlay_tenant_id)
    if fqn in tombstoned:  # object deleted in the working copy → no live deps either way
        return {
            "found": True, "fqn": fqn, "direction": direction, "overlay": True, "tombstoned": True,
            "depends_on": {"references": [], "related": []},
            "dependents": {"referenced_by": [], "related": []},
        }
    base = _deps_single(store, tenant_id, fqn, direction)
    ov = _deps_single(store, overlay_tenant_id, fqn, direction)
    owner_layer = "working" if fqn in touched else "release"
    src_out = ov if fqn in touched else base  # outgoing edges belong to fqn itself
    return {
        "found": True, "fqn": fqn, "direction": direction, "overlay": True,
        "depends_on": {
            "references": _tag_layer(src_out["out_refs"], owner_layer, tombstoned),
            "related": _tag_layer(src_out["out_related"], owner_layer, tombstoned),
        },
        "dependents": {
            "referenced_by": _merge_edge_rows(base["in_refs"], ov["in_refs"],
                                              src_key="fqn", touched=touched, tombstoned=tombstoned),
            "related": _merge_edge_rows(base["in_related"], ov["in_related"],
                                        src_key="fqn", touched=touched, tombstoned=tombstoned),
        },
    }


def _routine_fqn(chunk_fqn: str) -> str:
    """Strip a code chunk's part suffix to the routine address: 'M::Метод#code/1' -> 'M::Метод'."""
    return chunk_fqn.split("#code", 1)[0]


def _unit(r: dict[str, Any]) -> str:
    """Result identity: code chunks keep routine-level granularity (so a developer/reviewer
    gets a navigable `…ObjectModule::Метод`, with split parts collapsed to one routine);
    everything else collapses to the owner object."""
    if r.get("via") == "code" and r.get("chunk_fqn"):
        return _routine_fqn(r["chunk_fqn"])
    return r["fqn"]


def _dedup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the best-scoring chunk per unit (rows assumed sorted by score desc)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        u = _unit(r)
        if u in seen:
            continue
        seen.add(u)
        out.append(r)
    return out


def _rrf_fuse(sources: list[tuple[str, list[dict]]], top_k: int, rrf_k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion of several ranked, unit-deduplicated result lists."""
    scores: dict[str, float] = {}
    info: dict[str, dict[str, Any]] = {}
    for source, rows in sources:
        for rank, r in enumerate(rows):
            unit = _unit(r)
            scores[unit] = scores.get(unit, 0.0) + 1.0 / (rrf_k + rank)
            entry = info.setdefault(
                unit, {**{k: r.get(k) for k in ("fqn", "kind", "synonym")}, "sources": []}
            )
            if source not in entry["sources"]:
                entry["sources"].append(source)
            if "matched" not in entry:
                entry["matched"] = r.get("matched")
                entry["via"] = r.get("via")
                entry["corpus"] = r.get("source")  # config | its | artifact | platform_help | bsp_help
                entry["tenant"] = r.get("tenant")  # owning tenant (caller or shared) — for expand
                # For code units, surface the routine address; harmless otherwise.
                if r.get("via") == "code" and r.get("chunk_fqn"):
                    entry["routine_fqn"] = _routine_fqn(r["chunk_fqn"])
                    entry["routine"] = r.get("chunk_name")
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [{**info[unit], "rrf_score": round(score, 5)} for unit, score in ranked]


def _rerank(reranker, query: str, results: list[dict], top_k: int) -> list[dict]:
    pairs = [(query, r.get("matched") or r.get("synonym") or r["fqn"]) for r in results]
    for r, score in zip(results, reranker.score(pairs)):
        r["rerank_score"] = round(float(score), 5)
    results.sort(key=lambda r: r["rerank_score"], reverse=True)
    return results[:top_k]


def _resolve_routines(store, tenant_id: str, q: str) -> list[str]:
    """Resolve a routine reference (fqn | 'Module.Method' | name) to routine fqns."""
    if "::" in q:
        return [q]
    if "." in q:
        obj, _, meth = q.rpartition(".")
        rows = store.read(
            "MATCH (r:Routine {tenant_id: $t, name: $m}) WHERE r.object_fqn ENDS WITH $o "
            "RETURN r.fqn AS fqn",
            t=tenant_id, m=meth, o="." + obj,
        )
        return [x["fqn"] for x in rows]
    rows = store.read(
        "MATCH (r:Routine {tenant_id: $t, name: $q}) RETURN r.fqn AS fqn", t=tenant_id, q=q
    )
    return [x["fqn"] for x in rows]


def _callers_rows(store, tenant_id: str, fqns: list[str]) -> list[dict]:
    return store.read(
        "MATCH (caller:Routine)-[c:CALLS]->(r:Routine {tenant_id: $t}) WHERE r.fqn IN $fqns "
        "RETURN DISTINCT caller.fqn AS fqn, caller.name AS name, caller.object_fqn AS object, "
        "       caller.routine_kind AS kind, c.confidence AS confidence ORDER BY object, name",
        t=tenant_id, fqns=fqns,
    )


def _callees_rows(store, tenant_id: str, fqns: list[str]) -> list[dict]:
    return store.read(
        "MATCH (r:Routine {tenant_id: $t})-[c:CALLS]->(callee:Routine) WHERE r.fqn IN $fqns "
        "RETURN DISTINCT callee.fqn AS fqn, callee.name AS name, callee.object_fqn AS object, "
        "       callee.routine_kind AS kind, c.kind AS via ORDER BY object, name",
        t=tenant_id, fqns=fqns,
    )


def _routine_objects(store, tenant_id: str, fqns: list[str]) -> set[str]:
    """Owning object fqns of the given routines (for self-tombstone checks)."""
    if not fqns:
        return set()
    rows = store.read(
        "MATCH (r:Routine {tenant_id: $t}) WHERE r.fqn IN $f AND r.object_fqn IS NOT NULL "
        "RETURN DISTINCT r.object_fqn AS o",
        t=tenant_id, f=fqns,
    )
    return {r["o"] for r in rows if r.get("o")}


def find_callers(store, tenant_id: str, q: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Who calls a routine. With overlay: callers whose object is touched come from the overlay,
    unchanged callers from baseline (their code is unchanged); tombstoned callers are dropped. If the
    queried routine's own object is deleted in the working copy, returns empty (tombstoned)."""
    fqns = _resolve_routines(store, tenant_id, q)
    base = _callers_rows(store, tenant_id, fqns)
    if not overlay_tenant_id:
        return {"query": q, "routines": fqns, "callers": base, "count": len(base), "overlay": False}
    touched, tombstoned = _overlay_sets(store, overlay_tenant_id)
    objs = _routine_objects(store, tenant_id, fqns)
    if objs and objs <= tombstoned:  # the queried routine's object is deleted in the working copy
        return {"query": q, "routines": fqns, "callers": [], "count": 0,
                "tombstoned": True, "overlay": True}
    fqns = sorted(set(fqns) | set(_resolve_routines(store, overlay_tenant_id, q)))
    ov = _callers_rows(store, overlay_tenant_id, fqns)
    merged = _merge_edge_rows(base, ov, src_key="object", touched=touched, tombstoned=tombstoned)
    return {"query": q, "routines": fqns, "callers": merged, "count": len(merged), "overlay": True}


def find_callees(store, tenant_id: str, q: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """What a routine calls. With overlay: the working version's callees win (overlay), unioned with
    baseline callees for recall (overlay-internal call resolution is touched-scoped — see docs/OVERLAY.md).
    If the queried routine's own object is deleted in the working copy, returns empty (tombstoned)."""
    fqns = _resolve_routines(store, tenant_id, q)
    base = _callees_rows(store, tenant_id, fqns)
    if not overlay_tenant_id:
        return {"query": q, "routines": fqns, "callees": base, "count": len(base), "overlay": False}
    _, tombstoned = _overlay_sets(store, overlay_tenant_id)
    objs = _routine_objects(store, tenant_id, fqns)
    if objs and objs <= tombstoned:  # the queried routine's object is deleted in the working copy
        return {"query": q, "routines": fqns, "callees": [], "count": 0,
                "tombstoned": True, "overlay": True}
    ov_fqns = _resolve_routines(store, overlay_tenant_id, q)
    ov = _callees_rows(store, overlay_tenant_id, ov_fqns)
    out: dict[str, dict] = {}
    for r in ov:
        if r.get("fqn") in tombstoned:
            continue
        out[r["fqn"]] = {**r, "layer": "working"}
    for r in base:
        if r.get("fqn") in tombstoned:
            continue
        out.setdefault(r["fqn"], {**r, "layer": "release"})
    callees = sorted(out.values(), key=lambda r: (r.get("object") or "", r.get("name") or ""))
    return {"query": q, "routines": sorted(set(fqns) | set(ov_fqns)),
            "callees": callees, "count": len(callees), "overlay": True}


def call_graph(store, tenant_id: str, q: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Combined call graph around a routine: {callers, callees}. Overlay-aware (Phase 2)."""
    callers = find_callers(store, tenant_id, q, overlay_tenant_id)
    callees = find_callees(store, tenant_id, q, overlay_tenant_id)
    return {"query": q, "routines": callers.get("routines", []),
            "callers": callers.get("callers", []), "callees": callees.get("callees", []),
            "overlay": bool(overlay_tenant_id)}


def find_handlers(store, tenant_id: str, q: str) -> dict[str, Any]:
    """Behavior entry points of an object: form event handlers (via HANDLES) and standard
    module event routines (proving / write / fill-check / numbering …). For testers/reviewers."""
    head = _resolve(store, tenant_id, q)
    if head is None:
        return {"found": False, "query": q}
    fqn = head["fqn"]
    form_handlers = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:HAS_FORM]->(frm:Form)-[h:HANDLES]->(rt:Routine) "
        "RETURN frm.name AS form, rt.fqn AS routine_fqn, rt.name AS routine, "
        "       h.event AS event, h.element AS element ORDER BY form, event",
        t=tenant_id, fqn=fqn,
    )
    module_handlers = store.read(
        "MATCH (o:Object {tenant_id: $t, fqn: $fqn})-[:HAS_MODULE]->(m:Module)-[:DECLARES]->(rt:Routine) "
        "WHERE rt.entry_point IS NOT NULL "
        "RETURN m.module_type AS module, rt.fqn AS routine_fqn, rt.name AS routine, "
        "       rt.entry_point AS entry_point ORDER BY module, routine",
        t=tenant_id, fqn=fqn,
    )
    return {
        "found": True, "fqn": fqn,
        "module_handlers": module_handlers, "form_handlers": form_handlers,
        "count": len(module_handlers) + len(form_handlers),
    }


def find_overrides(store, tenant_id: str, q: str) -> dict[str, Any]:
    """Extension overrides of an object's BSL: routines annotated &Вместо/&Перед/&После/
    &ИзменениеИКонтроль in a borrowed (Adopted) object, each linked to the base method it hooks
    (OVERRIDES edge). For reviewers/architects auditing how extensions alter base behavior."""
    head = _resolve(store, tenant_id, q)
    if head is None:
        return {"found": False, "query": q}
    fqn = head["fqn"]
    rows = store.read(
        "MATCH (over:Routine {tenant_id: $t})-[o:OVERRIDES]->(base:Routine {tenant_id: $t}) "
        "WHERE over.object_fqn = $fqn OR base.object_fqn = $fqn "
        "RETURN o.mode AS mode, over.fqn AS override_routine, over.name AS override_name, "
        "       base.fqn AS base_routine, base.name AS base_method ORDER BY mode, override_name",
        t=tenant_id, fqn=fqn,
    )
    return {"found": True, "fqn": fqn, "overrides": rows, "count": len(rows)}


def _join_code_parts(rows: list[dict]) -> str:
    """Reconstruct routine source from its code-chunk rows [{fqn, text}]: order by the `#code/N`
    part index and drop each part's breadcrumb head line (everything up to the first newline).
    cAST splits on whole lines without truncation, so the concatenation is the verbatim body."""
    def _part_n(fqn: str) -> int:
        suffix = fqn.split("#code", 1)[1]
        return int(suffix[1:]) if suffix.startswith("/") and suffix[1:].isdigit() else 0

    ordered = sorted(rows, key=lambda r: _part_n(r["fqn"]))
    segments = [r["text"].split("\n", 1)[1] if "\n" in r["text"] else r["text"] for r in ordered]
    return "\n".join(segments)


def _reassemble_source(store, tenant_id: str, routine_fqn: str) -> str | None:
    """Rebuild a routine's verbatim body from its stored code chunks (`…::Метод#code[/N]`).
    Returns None if the routine has no code chunks (boilerplate below the threshold, or not vectorized)."""
    rows = store.read(
        "MATCH (c:Chunk {tenant_id: $t}) "
        "WHERE c.chunk_kind = 'code' AND (c.fqn = $base OR c.fqn STARTS WITH $prefix) "
        "RETURN c.fqn AS fqn, c.text AS text",
        t=tenant_id, base=f"{routine_fqn}#code", prefix=f"{routine_fqn}#code/",
    )
    return _join_code_parts(rows) if rows else None


def get_routine_source(store, tenant_id: str, q: str) -> dict[str, Any]:
    """Source of a BSL routine for agent context, including how extensions alter it. Resolves a
    routine (fqn | 'Module.Method' | name) and returns its base body PLUS every extension override
    (&Вместо/&Перед/&После/&ИзменениеИКонтроль) with its mode and source. Reassembled from stored
    code chunks (no dump files needed at query time). For partially-extended methods this gives the
    full picture: the base method and each hook — the platform composes them at runtime."""
    fqns = _resolve_routines(store, tenant_id, q)
    if not fqns:
        return {"found": False, "query": q}
    routines = []
    for fqn in fqns:
        meta = store.read(
            "MATCH (rt:Routine {tenant_id: $t, fqn: $f}) RETURN rt.name AS name, "
            "rt.object_fqn AS object, rt.module_type AS module_type, rt.entry_point AS entry_point, "
            "rt.override_mode AS override_mode, rt.override_target AS override_target LIMIT 1",
            t=tenant_id, f=fqn,
        )
        info = meta[0] if meta else {}
        ov = store.read(
            "MATCH (over:Routine {tenant_id: $t})-[o:OVERRIDES]->(:Routine {tenant_id: $t, fqn: $f}) "
            "RETURN o.mode AS mode, over.fqn AS fqn, over.object_fqn AS object ORDER BY o.mode",
            t=tenant_id, f=fqn,
        )
        overrides = [
            {"mode": r["mode"], "fqn": r["fqn"], "object": r["object"],
             "extension": r["fqn"].split("@ext:", 1)[1].split("::", 1)[0] if "@ext:" in r["fqn"] else None,
             "source": _reassemble_source(store, tenant_id, r["fqn"])}
            for r in ov
        ]
        routines.append({
            "fqn": fqn, "name": info.get("name"), "object": info.get("object"),
            "module_type": info.get("module_type"), "entry_point": info.get("entry_point"),
            "override_mode": info.get("override_mode"), "override_target": info.get("override_target"),
            "source": _reassemble_source(store, tenant_id, fqn),
            "overrides": overrides, "override_count": len(overrides),
        })
    return {"found": True, "query": q, "routines": routines, "count": len(routines)}


def find_related_docs(store, tenant_id: str, q: str) -> dict[str, Any]:
    """Documentation (ITS / project artifacts) linked to an object via MENTIONS (explicit/scanned
    fqns) or RELATES_TO (semantic). Answers 'what docs/standards cover this object'."""
    head = _resolve(store, tenant_id, q)
    if head is None:
        return {"found": False, "query": q}
    docs = store.read(
        "MATCH (d)-[r:MENTIONS|RELATES_TO]->(o:Object {tenant_id: $t, fqn: $fqn}) "
        "WHERE d:Document OR d:Artifact "
        "RETURN d.fqn AS fqn, labels(d)[0] AS label, d.source AS source, d.title AS title, "
        "       d.source_url AS source_url, type(r) AS rel, r.confidence AS confidence "
        "ORDER BY rel, confidence DESC, title",
        t=tenant_id, fqn=head["fqn"],
    )
    return {"found": True, "fqn": head["fqn"], "docs": docs, "count": len(docs)}


def get_document(store, tenant_id: str, fqn: str, shared_tenant_id: str | None = None) -> dict[str, Any]:
    """Full document by owner fqn (e.g. 'its:art-1' / 'platform_help:8.3.27|Массив.Найти'):
    metadata, full text (chunks rejoined), and the config objects it links to. Resolves in the
    caller tenant + the shared public tenant (so platform/BSP help docs are reachable)."""
    tenants = [tenant_id] + ([shared_tenant_id] if shared_tenant_id and shared_tenant_id != tenant_id else [])
    rows = store.read(
        "MATCH (d {fqn: $fqn}) WHERE d.tenant_id IN $tenants AND (d:Document OR d:Artifact) "
        "RETURN d.tenant_id AS tenant, labels(d)[0] AS label, properties(d) AS props "
        "ORDER BY CASE WHEN d.tenant_id = $caller THEN 0 ELSE 1 END LIMIT 1",  # caller wins on fqn clash
        tenants=tenants, fqn=fqn, caller=tenant_id,
    )
    if not rows:
        return {"found": False, "fqn": fqn}
    owner = rows[0]["tenant"]  # pin chunk/link reads to the resolved owner tenant (deterministic)
    chunks = store.read(
        "MATCH (d {tenant_id: $owner, fqn: $fqn})-[:HAS_CHUNK]->(c:Chunk) "
        "RETURN c.fqn AS fqn, c.text AS text ORDER BY c.fqn",
        owner=owner, fqn=fqn,
    )
    links = store.read(
        "MATCH (d {tenant_id: $owner, fqn: $fqn})-[r:MENTIONS|RELATES_TO]->(o:Object) "
        "RETURN type(r) AS rel, o.fqn AS object, r.confidence AS confidence ORDER BY rel, object",
        owner=owner, fqn=fqn,
    )
    props = dict(rows[0]["props"])
    for k in ("tenant_id", "config_id"):
        props.pop(k, None)
    return {
        "found": True, "fqn": fqn, "label": rows[0]["label"],
        "source": props.get("source"), "title": props.get("title"),
        "section_path": props.get("section_path"), "source_url": props.get("source_url"),
        "text": "\n\n".join(c["text"] for c in chunks),
        "links": links,
    }


def docinfo(store, tenant_id: str, name: str, platform_version: str | None = None,
            shared_tenant_id: str | None = None) -> dict[str, Any]:
    """Exact platform-help lookup by canonical name (RU / English / 'Object.Method'), version-aware.
    Resolves in caller + shared tenant. One match → full doc; several → disambiguation list."""
    tenants = [tenant_id] + ([shared_tenant_id] if shared_tenant_id and shared_tenant_id != tenant_id else [])
    n = name.strip().lower()
    cands = store.read(
        "MATCH (d) WHERE d.tenant_id IN $tenants AND d.source = 'platform_help' "
        "  AND ($pv IS NULL OR d.platform_version = $pv) "
        "  AND (d.full_name_norm = $n OR d.name_norm = $n OR toLower(coalesce(d.en_name, '')) = $n) "
        "RETURN d.fqn AS fqn, d.tenant_id AS tenant, d.title AS title, d.en_name AS en_name, "
        "       d.platform_version AS platform_version, d.help_kind AS help_kind, d.full_name_norm AS fn "
        "ORDER BY (CASE WHEN d.full_name_norm = $n THEN 0 ELSE 1 END), d.platform_version DESC "
        "LIMIT 25",
        tenants=tenants, n=n, pv=platform_version,
    )
    if not cands:
        return {"found": False, "name": name, "platform_version": platform_version}
    # collapse to one topic per (name, version); if a single topic remains, return its full doc
    distinct = {(c["fn"], c["platform_version"]): c for c in cands}
    if len(distinct) == 1:
        best = cands[0]
        doc = get_document(store, best["tenant"], best["fqn"], shared_tenant_id=shared_tenant_id)
        doc["platform_version"] = best["platform_version"]
        doc["help_kind"] = best["help_kind"]
        doc["en_name"] = best["en_name"]
        return doc
    return {"found": True, "name": name, "ambiguous": True,
            "candidates": [{"fqn": c["fqn"], "title": c["title"], "platform_version": c["platform_version"],
                            "help_kind": c["help_kind"]} for c in cands]}


def call_path(store, tenant_id: str, src: str, dst: str, max_hops: int = 8) -> dict[str, Any]:
    src_fqns = _resolve_routines(store, tenant_id, src)
    dst_fqns = _resolve_routines(store, tenant_id, dst)
    if not src_fqns or not dst_fqns:
        return {"from": src, "to": dst, "found": False, "path": []}
    rows = store.read(
        f"MATCH (a:Routine {{tenant_id: $t}}), (b:Routine {{tenant_id: $t}}) "
        "WHERE a.fqn IN $s AND b.fqn IN $d "
        f"MATCH p = shortestPath((a)-[:CALLS*..{max_hops}]->(b)) "
        "RETURN [n IN nodes(p) | n.fqn] AS path ORDER BY length(p) LIMIT 1",
        t=tenant_id, s=src_fqns, d=dst_fqns,
    )
    if not rows:
        return {"from": src, "to": dst, "found": False, "path": []}
    return {"from": src, "to": dst, "found": True, "path": rows[0]["path"]}


def _expand(store, tenant_id: str, results: list[dict]) -> list[dict]:
    """GraphRAG enrichment: attach a compact neighborhood to each hit so callers get a context
    bundle, not a bare reference. Objects → attribute count, subsystems, reference deps, movements;
    code → owning object, entry point, a few callers/callees. One small query per top-k hit."""
    for r in results:
        if r.get("via") == "code" and r.get("routine_fqn"):
            rows = store.read(
                "MATCH (rt:Routine {tenant_id: $t, fqn: $f}) "
                "OPTIONAL MATCH (rt)-[:CALLS]->(callee:Routine) "
                "OPTIONAL MATCH (caller:Routine)-[:CALLS]->(rt) "
                "RETURN rt.object_fqn AS object, rt.entry_point AS entry_point, "
                "       collect(DISTINCT callee.fqn)[..8] AS calls, "
                "       collect(DISTINCT caller.fqn)[..8] AS called_by",
                t=tenant_id, f=r["routine_fqn"],
            )
            r["context"] = rows[0] if rows else {}
        elif r.get("corpus") and r.get("corpus") != "config":
            # doc hit (its / artifact / platform_help / bsp_help / …) → linked config objects.
            # Use the hit's OWN tenant (doc may live in the shared public tenant, not the caller's).
            rows = store.read(
                "MATCH (d {tenant_id: $dt, fqn: $f})-[rel:MENTIONS|RELATES_TO]->(o:Object) "
                "WHERE d:Document OR d:Artifact "
                "RETURN type(rel) AS rel, o.fqn AS object ORDER BY rel, object",
                dt=r.get("tenant") or tenant_id, f=r["fqn"],
            )
            r["context"] = {"links": rows}
        else:
            rows = store.read(
                "MATCH (o:Object {tenant_id: $t, fqn: $f}) "
                "OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE|HAS_DIMENSION|HAS_RESOURCE]->(fld:Field) "
                "WITH o, count(DISTINCT fld) AS attrs "
                "OPTIONAL MATCH (sub:Object {tenant_id: $t, kind: 'Subsystem'})-[:CONTAINS]->(o) "
                "WITH o, attrs, collect(DISTINCT sub.name) AS subs "
                "OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE|HAS_DIMENSION|HAS_RESOURCE|HAS_TABULAR_SECTION*1..2]"
                "->(:Field)-[:REFERENCES]->(dep:Object) "
                "WITH o, attrs, subs, collect(DISTINCT dep.fqn)[..10] AS refs "
                "OPTIONAL MATCH (o)-[:WRITES_TO]->(reg:Object) "
                "RETURN attrs AS attribute_count, subs AS subsystems, refs AS references, "
                "       collect(DISTINCT reg.fqn) AS writes_to",
                t=tenant_id, f=r["fqn"],
            )
            r["context"] = rows[0] if rows else {}
    return results


def metrics(store, tenant_id: str, subsystem: str | None = None) -> dict[str, Any]:
    """Inventory & hotspot metrics for an overview: object counts by kind, code volume, call-graph
    edges by kind/confidence, fan-in/out hotspots, behavior entry points. Optionally scoped to a
    subsystem (and its descendants)."""
    if subsystem:
        by_kind = store.read(
            "MATCH (s:Object {tenant_id: $t, kind: 'Subsystem'}) WHERE s.name = $s OR s.fqn = $s "
            "MATCH (s)-[:HAS_SUBSYSTEM*0..]->(:Object)-[:CONTAINS]->(o:Object) "
            "WHERE coalesce(o.stub, false) = false "
            "RETURN o.kind AS kind, count(DISTINCT o) AS n ORDER BY n DESC",
            t=tenant_id, s=subsystem,
        )
    else:
        by_kind = store.read(
            "MATCH (o:Object {tenant_id: $t}) WHERE coalesce(o.stub, false) = false "
            "RETURN o.kind AS kind, count(*) AS n ORDER BY n DESC",
            t=tenant_id,
        )
    routines = store.read("MATCH (rt:Routine {tenant_id: $t}) RETURN count(rt) AS n", t=tenant_id)
    code_bytes = store.read(
        "MATCH (:Object {tenant_id: $t})-[:HAS_MODULE]->(m:Module) RETURN sum(coalesce(m.size, 0)) AS b",
        t=tenant_id,
    )
    calls_by_kind = store.read(
        "MATCH (:Routine {tenant_id: $t})-[c:CALLS]->() "
        "RETURN c.kind AS kind, c.confidence AS confidence, count(*) AS n ORDER BY n DESC",
        t=tenant_id,
    )
    entry_points = store.read(
        "MATCH (rt:Routine {tenant_id: $t}) WHERE rt.entry_point IS NOT NULL "
        "RETURN rt.entry_point AS entry_point, count(*) AS n ORDER BY n DESC",
        t=tenant_id,
    )
    by_config = store.read(
        "MATCH (o:Object {tenant_id: $t}) WHERE coalesce(o.stub, false) = false "
        "RETURN o.config_id AS config_id, count(*) AS n ORDER BY n DESC",
        t=tenant_id,
    )
    overrides = store.read(  # extension overrides of borrowed objects' BSL (&Вместо/&Перед/…)
        "MATCH (:Routine {tenant_id: $t})-[r:OVERRIDES]->() "
        "RETURN r.mode AS mode, count(*) AS n ORDER BY n DESC",
        t=tenant_id,
    )
    top_fan_in = store.read(
        "MATCH (caller:Routine {tenant_id: $t})-[:CALLS]->(rt:Routine) "
        "RETURN rt.fqn AS routine, count(DISTINCT caller) AS fan_in ORDER BY fan_in DESC LIMIT 10",
        t=tenant_id,
    )
    top_fan_out = store.read(
        "MATCH (rt:Routine {tenant_id: $t})-[:CALLS]->(callee:Routine) "
        "RETURN rt.fqn AS routine, count(DISTINCT callee) AS fan_out ORDER BY fan_out DESC LIMIT 10",
        t=tenant_id,
    )
    return {
        "tenant_id": tenant_id,
        "subsystem": subsystem,
        "objects_by_kind": by_kind,
        "objects_total": sum(r["n"] for r in by_kind),
        "routines": routines[0]["n"] if routines else 0,
        "code_bytes": code_bytes[0]["b"] if code_bytes else 0,
        "calls_by_kind": calls_by_kind,
        "entry_points": entry_points,
        "objects_by_config_id": by_config,
        "extension_overrides": overrides,
        "hotspots": {"top_fan_in": top_fan_in, "top_fan_out": top_fan_out},
    }


def _fts_query(raw: str) -> str:
    """Build a Lucene query from natural text: split identifiers into sub-word tokens (so
    'Продажи' matches 'ПродажиТоваров'), OR them together. search_tokens keeps only word
    characters, so the result is free of Lucene special chars (no escaping needed)."""
    from .chunking import search_tokens

    return search_tokens(raw) or raw


# Filtered searches use exact cosine over the candidate set (perfect recall) when that set is
# small; above this many candidates we fall back to the vector index + post-filter (so a huge
# filter like chunk_kinds=['code'] on a big tenant stays fast).
_EXACT_SCAN_CAP = 50000


def _vector_retrievers(store, tenant_id, vec, fetch, f, shared_tenant_id=None):
    """Return (semantic, ident) deduped hit lists, choosing exact vs index retrieval per the
    filter selectivity. `shared_tenant_id` (public corpus tenant) is read additively but does
    NOT count toward filter selectivity (it must not force the exact-scan path)."""
    use_exact = False
    if any(f.values()):
        use_exact = store.filtered_chunk_count(
            tenant_id, _EXACT_SCAN_CAP, shared_tenant_id=shared_tenant_id, **f) < _EXACT_SCAN_CAP
    search = store.exact_vector_search if use_exact else store.vector_search
    sem = _dedup(search(tenant_id, vec, fetch, index="chunk_embedding", shared_tenant_id=shared_tenant_id, **f))
    idt = _dedup(search(tenant_id, vec, fetch, index="chunk_embedding_ident", shared_tenant_id=shared_tenant_id, **f))
    return sem, idt


def semantic_search(store, tenant_id, query, embedder, top_k=10, overfetch=5,
                    kinds=None, chunk_kinds=None, subsystem=None, source=None, platform_version=None,
                    doc_topic=None, corpus_version=None, help_kind=None,
                    expand=False, shared_tenant_id=None):
    """Multi-vector semantic search (meaning × identifier), fused with RRF.

    Optional filters (source / kinds / chunk_kinds / subsystem / platform_version / doc_topic /
    corpus_version / help_kind) are post-applied to the vector hits; fetch is widened when filtering
    so a narrow slice still fills top_k. doc_topic / corpus_version / help_kind are owner-node facets
    (apply only with the matching source). With expand=True each hit is enriched with a compact graph
    neighborhood (GraphRAG). shared_tenant_id adds a public corpus tenant to the read scope (server-derived)."""
    vec = embedder.embed([query], is_query=True)[0]
    filtered = bool(kinds or chunk_kinds or subsystem or source or platform_version
                    or doc_topic or corpus_version or help_kind)
    fetch = top_k * overfetch * (4 if filtered else 1)
    f = dict(kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
             platform_version=platform_version, doc_topic=doc_topic,
             corpus_version=corpus_version, help_kind=help_kind)
    sem, idt = _vector_retrievers(store, tenant_id, vec, fetch, f, shared_tenant_id)
    results = _rrf_fuse([("semantic", sem), ("ident", idt)], top_k)
    if expand:
        _expand(store, tenant_id, results)
    return {"query": query, "mode": "semantic", "results": results}


def hybrid_search(store, tenant_id, query, embedder, top_k=10, overfetch=5, rrf_k=60,
                  reranker=None, kinds=None, chunk_kinds=None, subsystem=None, source=None,
                  platform_version=None, doc_topic=None, corpus_version=None, help_kind=None,
                  expand=False, shared_tenant_id=None):
    """Multi-vector (meaning × identifier) + full-text, fused with RRF; optional rerank.

    Optional filters (source / kinds / chunk_kinds / subsystem / platform_version / doc_topic /
    corpus_version / help_kind) restrict all three retrievers. doc_topic / corpus_version / help_kind
    are owner-node facets (apply only with the matching source). With expand=True each hit is enriched
    with a compact graph neighborhood (GraphRAG). shared_tenant_id adds a public corpus tenant to the
    read scope (server-derived)."""
    vec = embedder.embed([query], is_query=True)[0]
    filtered = bool(kinds or chunk_kinds or subsystem or source or platform_version
                    or doc_topic or corpus_version or help_kind)
    fetch = top_k * overfetch * (4 if filtered else 1)
    f = dict(kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
             platform_version=platform_version, doc_topic=doc_topic,
             corpus_version=corpus_version, help_kind=help_kind)
    sem, idt = _vector_retrievers(store, tenant_id, vec, fetch, f, shared_tenant_id)
    ft = _dedup(store.fulltext_search(tenant_id, _fts_query(query), limit=fetch, shared_tenant_id=shared_tenant_id, **f))
    pool = top_k if reranker is None else max(top_k, 20)
    results = _rrf_fuse([("semantic", sem), ("ident", idt), ("fulltext", ft)], pool, rrf_k)
    if reranker is not None:
        results = _rerank(reranker, query, results, top_k)
    if expand:
        _expand(store, tenant_id, results)
    return {"query": query, "mode": "hybrid", "results": results}
