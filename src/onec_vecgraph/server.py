"""MCP server (FastMCP).

Multi-tenant over Streamable HTTP: each call's tenant/config is resolved from the
request headers (X-Tenant-Id / X-Config-Id) and enforced — no silent fallback to a
shared default (prevents cross-company access). stdio (local dev) uses configured defaults.
"""

from __future__ import annotations

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
def get_dependencies(ctx: Context, query: str, direction: str = "both") -> dict[str, Any]:
    """Dependency graph around an object. direction: 'out' (what it depends on), 'in' (what depends
    on it), or 'both'. Returns depends_on/dependents each with 'references' (type refs) and 'related'
    (CONTAINS/OWNED_BY/SUBSCRIBES/HAS_RIGHT_ON/WRITES_TO/... edges, labelled by 'rel')."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.get_dependencies(store, _tenant(ctx), query, direction)


@mcp.tool()
def impact_analysis(ctx: Context, query: str) -> dict[str, Any]:
    """What would be affected if this object changes: incoming references, subsystems, roles and subscriptions that depend on it."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.get_dependencies(store, _tenant(ctx), query, "in")


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
    metadata, full text (chunks rejoined) and the config objects it links to."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.get_document(store, _tenant(ctx), fqn)


# ── search ────────────────────────────────────────────────────────────
@mcp.tool()
def semantic_search(
    ctx: Context, query: str, top_k: int = 10, kinds: list[str] | None = None,
    chunk_kinds: list[str] | None = None, subsystem: str | None = None,
    source: list[str] | None = None, expand: bool = False,
) -> dict[str, Any]:
    """Semantic (multi-vector) search over the indexed corpora by meaning.

    Optional filters: source (corpus: ['config','its','artifact'] — config = the 1C configuration,
    its = 1C ITS docs, artifact = project docs), kinds (object kinds like
    ['Catalog','Document','Subsystem']), chunk_kinds (['object','attribute','code','form',
    'enum_value','predefined','subsystem','role']), subsystem (name/fqn — restrict to that subsystem
    or its descendants). Code hits return routine granularity ('routine_fqn'); each hit carries
    'corpus'. expand=True attaches a compact graph neighborhood ('context') to each hit (GraphRAG).
    Returns: {query, mode, results:[{fqn, kind, synonym, via, corpus, matched, rrf_score,
    routine_fqn?/routine? (code), context? (expand)}]}. Requires prior vectorization."""
    from .embeddings.runtime import provider

    with Neo4jStore.from_settings(settings) as store:
        return queries.semantic_search(
            store, _tenant(ctx), query, provider(settings), top_k,
            kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source, expand=expand,
        )


@mcp.tool()
def hybrid_search(
    ctx: Context, query: str, top_k: int = 10, kinds: list[str] | None = None,
    chunk_kinds: list[str] | None = None, subsystem: str | None = None,
    source: list[str] | None = None, expand: bool = False,
) -> dict[str, Any]:
    """Hybrid search (multi-vector + full-text + RRF). Best for mixed meaning/identifier queries.

    Same optional filters as semantic_search (source / kinds / chunk_kinds / subsystem / expand).
    source selects corpora (['config','its','artifact']). Identifiers are sub-word tokenized, so
    'Продажи' matches 'ПродажиТоваров'. Code hits are routine-grained; each hit carries 'corpus'.
    Same result shape as semantic_search. Requires prior vectorization."""
    from .embeddings.runtime import provider, reranker

    with Neo4jStore.from_settings(settings) as store:
        return queries.hybrid_search(
            store, _tenant(ctx), query, provider(settings), top_k, reranker=reranker(settings),
            kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source, expand=expand,
        )


@mcp.tool()
def metrics(ctx: Context, subsystem: str | None = None) -> dict[str, Any]:
    """Inventory & hotspot metrics: object counts by kind, code volume, call-graph edges by
    kind/confidence, fan-in/out hotspots, behavior entry points. Optionally scoped to a subsystem."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.metrics(store, _tenant(ctx), subsystem)


# ── BSL call graph ────────────────────────────────────────────────────
@mcp.tool()
def find_callers(ctx: Context, query: str) -> dict[str, Any]:
    """Which BSL routines call the given procedure/function (by routine fqn, 'Module.Method', or
    bare name). Returns {query, routines, callers:[{fqn, name, object, kind, confidence}], count}.
    Requires the BSL call graph to have been built for the tenant."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_callers(store, _tenant(ctx), query)


@mcp.tool()
def find_callees(ctx: Context, query: str) -> dict[str, Any]:
    """Which BSL routines the given procedure/function calls. Returns {query, routines,
    callees:[{fqn, name, object, kind, via (local/common_module/manager)}], count}.
    Requires the BSL call graph to have been built for the tenant."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_callees(store, _tenant(ctx), query)


@mcp.tool()
def call_path(ctx: Context, from_routine: str, to_routine: str) -> dict[str, Any]:
    """Shortest BSL call path between two routines."""
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
