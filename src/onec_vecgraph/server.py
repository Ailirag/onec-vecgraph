"""MCP server (FastMCP).

Multi-tenant over Streamable HTTP: each call's tenant/config is resolved from the
request headers (X-Tenant-Id / X-Config-Id) and enforced — no silent fallback to a
shared default (prevents cross-company access). stdio (local dev) uses configured defaults.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import __version__, queries, tenancy
from .config import get_settings
from .storage import Neo4jStore

settings = get_settings()

# Server-level instructions: sent to every MCP client on `initialize`. This is the overview a
# cold third-party agent needs to use the server correctly (tenant header, fqn convention, which
# tool for which need, data-availability caveat). Per-tool docstrings cover the specifics.
INSTRUCTIONS = """\
onec-vecgraph — READ-ONLY knowledge base over a 1C:Enterprise configuration (parsed from a
Configurator XML dump) stored in Neo4j: metadata graph + BSL dependency/call graph + vector &
full-text search. Tools never modify the configuration; they answer questions about it.

ACCESS — multi-tenant. Over HTTP every call MUST carry the header `X-Tenant-Id: <tenant>`
(optionally `X-Config-Id`); without it the request is REJECTED (no shared default). Call `whoami`
to confirm the resolved tenant. A tenant only has data for the layers that were built for it:
metadata graph + raw `:Detail` properties (from indexing) are usually present; vector search needs
prior vectorization; the BSL call graph / `find_handlers` need the call graph to have been built.
EMPTY RESULTS usually mean that layer isn't built for this tenant — not that nothing matched.

OBJECT IDENTITY (fqn) = `<Kind>.<Name>`, e.g. `Catalog.Контрагенты`, `Document.РеализацияТоваров`,
`Enum.СтатусыЗаказов`, `CommonModule.ОбщегоНазначения`, `InformationRegister.Цены`,
`AccumulationRegister.Продажи`, `Subsystem.Продажи`, `Role.Администратор`. A routine fqn is
`<owner-module-fqn>::<MethodName>`. Most tools accept either an fqn or a bare object name.

WHICH TOOL (by need):
• Find by meaning/keywords → hybrid_search (default; also matches identifiers like 'ПродажиТоваров')
  or semantic_search. Narrow with kinds / chunk_kinds / subsystem; expand=True adds a graph
  neighborhood per hit. Code hits carry `routine_fqn` (feed it to find_callers/callees).
• Browse/list → list_metadata. Inventory & hotspots → metrics.
• Object structure (light, semantic) → get_object. Full raw config/UI properties (Hierarchical,
  Posting, CodeLength, Periodicity, lock mode, ...) → get_object_properties or get_object(detail=True).
• Relationships → get_dependencies / impact_analysis (who breaks if it changes) / find_type_usages.
• Behavior & code → find_handlers (what runs on posting/writing/validation + form events),
  find_callers / find_callees / call_path (BSL call graph).
• Documentation → search with source=['its'|'artifact'] (1C ITS / project docs, if ingested);
  find_related_docs (docs linked to an object) / get_document (full doc by fqn). Search hits carry
  a 'corpus' field; filter by source to keep config and docs separate.
• 1C development STANDARDS ("как писать по стандартам 1С": naming/coding conventions, event handlers,
  query rules) → search_standards (ranked search over the v8std corpus) then get_standard(<number>)
  for one standard's full text. Always available — the standards live in the shared public tenant.
• Platform/BSP help (syntax assistant) → docinfo (exact name lookup, e.g. 'Массив.Найти' / 'Array.Find')
  or search with source=['platform_help']. These live in the shared public tenant and are VERSION-AWARE:
  pass platform_version (e.g. '8.3.27.2130') to pin a build — use it ONLY together with
  source=['platform_help'] (it filters by the doc's version, so it would drop config results, which have
  none). Omit it to span all loaded versions (docinfo then returns per-version 'candidates' when several
  match; a single match returns the article). get_document fqn encodes the version: 'platform_help:<ver>|<Name>'.
• Doc-classification facets (search filters on the owner node — pair with the matching source, else config
  drops out): doc_topic ('platform' | 'config' | 'task') separates platform vs configuration vs task docs;
  corpus_version (typed, e.g. 'config:ERP_2.5.18') pins a configuration release; help_kind ('context' |
  'language' | 'query') narrows syntax-assistant help. These classify; tenant still controls isolation.

DATA-DEPTH LAYERS (cheap → exhaustive): search & list (discovery) → get_object (semantic structure)
→ get_object_properties / :Detail (every raw property). The detail layer is deliberately NOT
searchable — fetch it by fqn when you need exact configuration facts."""

mcp = FastMCP(
    "onec-vecgraph",
    instructions=INSTRUCTIONS,
    host=settings.mcp_host,
    port=settings.mcp_port,
    streamable_http_path=settings.mcp_path,
    stateless_http=True,  # tenant-per-request; no shared session state
)


def _tenant(ctx: Context) -> str:
    """Resolve and return the caller's tenant id (raises if missing over HTTP)."""
    return tenancy.resolve(ctx, settings).tenant_id


def _shared(tenant_id: str) -> str | None:
    """Server-derived public-corpus tenant to read additively (None if disabled or caller==shared).
    Never client-controlled — prevents reading arbitrary tenants."""
    s = settings.shared_tenant_id
    return s if settings.include_shared_tenant and s and s != tenant_id else None


def _overlay(caller_tenant: str, overlay_tenant_id: str | None) -> str | None:
    """Validate a client-supplied overlay tenant against the caller's namespace (anti-leak).

    A caller may union its own baseline only with an overlay UNDER that baseline ('<base>@task/*').
    Any other overlay_tenant_id is rejected, so this query arg cannot read foreign tenants."""
    if not overlay_tenant_id:
        return None
    from .overlay import base_tenant_of, in_namespace

    base = base_tenant_of(caller_tenant)  # caller may itself be an overlay → compare against its base
    if not in_namespace(overlay_tenant_id, base):
        raise ValueError(
            f"overlay_tenant_id {overlay_tenant_id!r} is not an overlay under the caller's base {base!r}"
        )
    return overlay_tenant_id


# ── health / introspection ────────────────────────────────────────────
@mcp.tool()
def ping() -> dict[str, Any]:
    """Liveness check. Returns server name and version."""
    return {"status": "ok", "server": "onec-vecgraph", "version": __version__}


@mcp.tool()
def neo4j_health() -> dict[str, Any]:
    """Check Neo4j connectivity and report server edition and node count."""
    with Neo4jStore.from_settings(settings) as store:
        return store.health()


@mcp.tool()
def whoami(ctx: Context) -> dict[str, Any]:
    """Return the tenant/config resolved for this request (to verify header wiring)."""
    scope = tenancy.resolve(ctx, settings)
    return {"tenant_id": scope.tenant_id, "config_id": scope.config_id}


# ── metadata / graph ──────────────────────────────────────────────────
@mcp.tool()
def list_metadata(
    ctx: Context, kind: str | None = None, name_contains: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """List metadata objects, optionally filtered by kind and name/synonym substring.

    kind ∈ Catalog, Document, Enum, InformationRegister, AccumulationRegister, AccountingRegister,
    CommonModule, Report, DataProcessor, Constant, ChartOfCharacteristicTypes, Subsystem, Role,
    EventSubscription, BusinessProcess, Task, ExchangePlan, ... Returns: [{fqn, kind, name, synonym,
    config_id}]. For semantic discovery use hybrid_search instead."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.list_metadata(store, _tenant(ctx), kind, name_contains, limit)


@mcp.tool()
def get_object(ctx: Context, query: str, detail: bool = False) -> dict[str, Any]:
    """Full card for an object (by fqn like 'Catalog.AI_Модели' or by name): attributes with types, tabular sections, enum values, predefined values, forms, modules, owners, subsystems.

    detail=True additionally returns 'details' — the full raw metadata property set (every
    <Properties>: Hierarchical, CodeLength, Posting, Periodicity, full-text search, lock mode,
    standard attributes, ...) for developer/analyst deep-dives."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.get_object(store, _tenant(ctx), query, detail=detail)


@mcp.tool()
def get_object_properties(ctx: Context, query: str) -> dict[str, Any]:
    """Full raw metadata property set for an object (by fqn or name): every <Properties> value —
    Hierarchical, CodeLength/CodeType, NumberLength, Posting/RealTimePosting, Periodicity,
    WriteMode, FullTextSearch, DataLockControlMode, ChoiceMode, standard attributes, etc.
    For developer/analyst deep-dives; these are stored but deliberately not vectorized."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.get_object_properties(store, _tenant(ctx), query)


@mcp.tool()
def get_dependencies(ctx: Context, query: str, direction: str = "both",
                     overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Dependency graph around an object. direction: 'out' (what it depends on), 'in' (what depends
    on it), or 'both'. Returns depends_on/dependents each with 'references' (type refs) and 'related'
    (CONTAINS/OWNED_BY/SUBSCRIBES/HAS_RIGHT_ON/WRITES_TO/... edges, labelled by 'rel').
    overlay_tenant_id ('<base>@task/<id>' under the caller's tenant) unions baseline ∪ working copy:
    outgoing edges follow the object's live version, incoming edges merge by source ownership,
    tombstones mask deletions; rows carry 'layer' (release/working)."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.get_dependencies(store, t, query, direction,
                                        overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool()
def impact_analysis(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """What would be affected if this object changes: incoming references, subsystems, roles and
    subscriptions that depend on it. overlay_tenant_id ('<base>@task/<id>' under the caller's tenant)
    unions baseline ∪ working copy with tombstone masking and 'layer' provenance (Phase 2)."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.get_dependencies(store, t, query, "in",
                                        overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool()
def find_type_usages(ctx: Context, query: str) -> dict[str, Any]:
    """Find all attributes/dimensions/resources that use the given object as their reference type."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_type_usages(store, _tenant(ctx), query)


@mcp.tool()
def find_related_docs(ctx: Context, query: str) -> dict[str, Any]:
    """Documentation (ITS / project artifacts) linked to an object (by fqn or name) via MENTIONS
    (explicit/scanned fqns) or RELATES_TO (semantic, with confidence). Answers 'what standards/docs
    cover this object'. Returns docs:[{fqn, label, source, title, source_url, rel, confidence}].
    Requires the corresponding corpus to have been ingested (see the `ingest` pipeline)."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_related_docs(store, _tenant(ctx), query)


@mcp.tool()
def get_document(ctx: Context, fqn: str) -> dict[str, Any]:
    """Full document by owner fqn ('its:<id>' / 'artifact:<path>#<n>', e.g. from a search hit's fqn):
    metadata, full text (chunks rejoined) and the config objects it links to. Resolves in the
    caller tenant and the shared public tenant (platform/BSP help)."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.get_document(store, t, fqn, shared_tenant_id=_shared(t))


@mcp.tool()
def docinfo(ctx: Context, name: str, platform_version: str | None = None) -> dict[str, Any]:
    """Exact 1C platform-help lookup by canonical name — the syntax assistant. Accepts a Russian
    name, the English name, or the dotted 'Object.Method'/'Object.Property' form (e.g.
    'ТаблицаЗначений', 'Массив.Найти', 'QuerySchema'). Optional platform_version (e.g. '8.3.27.1989')
    selects a build; otherwise the latest indexed wins. One match → full help article; several →
    a 'candidates' list to disambiguate. Reads the shared public tenant (no extra args)."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.docinfo(store, t, name, platform_version=platform_version, shared_tenant_id=_shared(t))


# ── 1C development standards (v8std) ───────────────────────────────────
_STD_NUM_RE = re.compile(r"\d+")


def _standard_fqn(standard: str) -> str:
    """Normalize a standard reference to a document fqn 'its:<id>'.

    Accepts a bare number ('396'), an anchor ('std396' / '#std396'), the external id
    ('v8std_396'), or an already-qualified fqn ('its:v8std_396')."""
    s = (standard or "").strip()
    if s.startswith("its:"):
        return s
    if s.lower().startswith(settings.standards_id_prefix.lower()):
        return f"its:{s}"
    m = _STD_NUM_RE.search(s)
    return f"its:{settings.standards_id_prefix}{m.group(0)}" if m else f"its:{s}"


@mcp.tool()
def search_standards(ctx: Context, query: str, top_k: int = 8, expand: bool = False) -> dict[str, Any]:
    """Search the 1C:Enterprise DEVELOPMENT STANDARDS (official «Система стандартов и методик разработки
    конфигураций», ITS v8std) by meaning or keywords: naming/coding conventions, event-handler rules,
    query standards, module structure, common-module usage, etc. Use this whenever you need "how should
    this be done per 1C standards" guidance for writing or reviewing configuration code.

    Returns ranked hits; each carries the standard's fqn ('its:<id>' — feed it to get_standard for the
    full text), title, section_path and source_url (its.1c.ru). expand=True attaches a graph
    neighborhood. The standards live in the shared public tenant and are read for EVERY tenant — no
    special access needed. (Thin wrapper over hybrid_search pinned to the standards corpus.)"""
    from .embeddings.runtime import provider, reranker

    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.hybrid_search(
            store, t, query, provider(settings), top_k, reranker=reranker(settings),
            source=["its"], corpus_version=settings.standards_corpus_version,
            expand=expand, shared_tenant_id=_shared(t),
        )


@mcp.tool()
def get_standard(ctx: Context, standard: str) -> dict[str, Any]:
    """Full text of ONE 1C development standard by its number or id. Accepts a bare number ('396'),
    an anchor ('std396' / '#std396'), the id ('v8std_396'), or a search hit's fqn ('its:v8std_396').
    Returns the standard's title, full text (chunks rejoined), section_path and source_url. Use after
    search_standards to read a specific standard end-to-end."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.get_document(store, t, _standard_fqn(standard), shared_tenant_id=_shared(t))


# ── search ────────────────────────────────────────────────────────────
@mcp.tool()
def semantic_search(
    ctx: Context, query: str, top_k: int = 10, kinds: list[str] | None = None,
    chunk_kinds: list[str] | None = None, subsystem: str | None = None,
    source: list[str] | None = None, platform_version: str | None = None,
    doc_topic: str | None = None, corpus_version: str | None = None, help_kind: str | None = None,
    expand: bool = False,
) -> dict[str, Any]:
    """Semantic (multi-vector) search over the indexed corpora by meaning.

    Optional filters: source (corpus: ['config','its','artifact','platform_help','bsp_help'] — config
    = the 1C configuration, its = 1C ITS docs, artifact = project docs, platform_help/bsp_help = public
    platform/library help from the shared tenant), platform_version (e.g. '8.3.27.1989' — restrict help
    to one build), kinds (object kinds like ['Catalog','Document','Subsystem']), chunk_kinds
    (['object','attribute','code','form','enum_value','predefined','subsystem','role']), subsystem
    (name/fqn). Doc-classification facets (owner-node — pair with the matching source, else config is
    dropped): doc_topic ('platform' | 'config' | 'task'), corpus_version (typed, e.g. 'config:ERP_2.5.18'
    / 'task:JIRA-1234'), help_kind ('context' | 'language' | 'query'). Public corpora are read additively
    from the shared tenant — no extra args. Code hits return routine granularity ('routine_fqn'); each
    hit carries 'corpus'. expand=True attaches a compact graph neighborhood ('context'). Requires prior
    vectorization."""
    from .embeddings.runtime import provider

    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.semantic_search(
            store, t, query, provider(settings), top_k,
            kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
            platform_version=platform_version, doc_topic=doc_topic, corpus_version=corpus_version,
            help_kind=help_kind, expand=expand, shared_tenant_id=_shared(t),
        )


@mcp.tool()
def hybrid_search(
    ctx: Context, query: str, top_k: int = 10, kinds: list[str] | None = None,
    chunk_kinds: list[str] | None = None, subsystem: str | None = None,
    source: list[str] | None = None, platform_version: str | None = None,
    doc_topic: str | None = None, corpus_version: str | None = None, help_kind: str | None = None,
    expand: bool = False,
) -> dict[str, Any]:
    """Hybrid search (multi-vector + full-text + RRF). Best for mixed meaning/identifier queries.

    Same optional filters as semantic_search (source / platform_version / kinds / chunk_kinds /
    subsystem / doc_topic / corpus_version / help_kind / expand). source selects corpora
    (['config','its','artifact','platform_help','bsp_help']); public corpora are read additively from
    the shared tenant. platform_version restricts help to one build; doc_topic/corpus_version/help_kind
    are owner-node facets (pair with the matching source). Identifiers are sub-word tokenized, so
    'Продажи' matches 'ПродажиТоваров'. Code hits are routine-grained; each hit carries 'corpus'. Same
    result shape as semantic_search."""
    from .embeddings.runtime import provider, reranker

    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.hybrid_search(
            store, t, query, provider(settings), top_k, reranker=reranker(settings),
            kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
            platform_version=platform_version, doc_topic=doc_topic, corpus_version=corpus_version,
            help_kind=help_kind, expand=expand, shared_tenant_id=_shared(t),
        )


@mcp.tool()
def metrics(ctx: Context, subsystem: str | None = None) -> dict[str, Any]:
    """Inventory & hotspot metrics: object counts by kind, code volume, call-graph edges by
    kind/confidence, fan-in/out hotspots, behavior entry points. Optionally scoped to a subsystem."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.metrics(store, _tenant(ctx), subsystem)


# ── BSL call graph ────────────────────────────────────────────────────
@mcp.tool()
def find_callers(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Which BSL routines call the given procedure/function (by routine fqn, 'Module.Method', or
    bare name). Returns {query, routines, callers:[{fqn, name, object, kind, confidence}], count}.
    overlay_tenant_id unions baseline ∪ working copy (callers tagged 'layer'). Requires the call graph."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.find_callers(store, t, query, overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool()
def find_callees(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Which BSL routines the given procedure/function calls. Returns {query, routines,
    callees:[{fqn, name, object, kind, via (local/common_module/manager)}], count}.
    overlay_tenant_id unions baseline ∪ working copy (callees tagged 'layer'). Requires the call graph."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.find_callees(store, t, query, overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool()
def call_graph(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Combined BSL call graph around a routine: {callers, callees}. With overlay_tenant_id
    ('<base>@task/<id>' under the caller's tenant) the result unions baseline ∪ working copy
    (overlay wins, tombstones mask deletions; rows carry 'layer'). Requires the call graph."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.call_graph(store, t, query, overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool()
def call_path(ctx: Context, from_routine: str, to_routine: str) -> dict[str, Any]:
    """Shortest BSL call path between two routines (single tenant; for overlay-aware analysis use
    call_graph — cross-layer path-finding is not unioned, see docs/OVERLAY.md)."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.call_path(store, _tenant(ctx), from_routine, to_routine)


@mcp.tool()
def find_handlers(ctx: Context, query: str) -> dict[str, Any]:
    """Behavior entry points of an object (by fqn or name): form event handlers (event→routine,
    via HANDLES) and standard module events (проведение/запись/проверка_заполнения/нумерация/…).
    Answers 'what runs when this is posted/written/validated' and 'which form events exist'."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_handlers(store, _tenant(ctx), query)


def run(transport: str = "streamable-http") -> None:
    mcp.run(transport=transport)
