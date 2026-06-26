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
onec-vecgraph — READ-ONLY база знаний по конфигурации 1С:Предприятие (разобранной из XML-выгрузки
Конфигуратора), хранится в Neo4j: граф метаданных + граф зависимостей/вызовов BSL + векторный и
полнотекстовый поиск. Инструменты никогда не изменяют конфигурацию; они отвечают на вопросы о ней.

ДОСТУП — мультиарендный. По HTTP каждый вызов ОБЯЗАН нести заголовок `X-Tenant-Id: <tenant>`
(опц. `X-Config-Id`); без него запрос ОТКЛОНЯЕТСЯ (общего дефолта нет). Вызовите `whoami`, чтобы
подтвердить определённого арендатора. У арендатора есть данные только тех слоёв, что для него
построены: граф метаданных + сырые свойства `:Detail` (из индексации) обычно есть; векторный поиск
требует предварительной векторизации; граф вызовов BSL / `find_handlers` требуют построенного графа
вызовов. ПУСТЫЕ РЕЗУЛЬТАТЫ обычно означают, что слой не построен для этого арендатора, а не что ничего
не нашлось.

ИДЕНТИЧНОСТЬ ОБЪЕКТА (fqn) = `<Вид>.<Имя>`, напр. `Catalog.Контрагенты`, `Document.РеализацияТоваров`,
`Enum.СтатусыЗаказов`, `CommonModule.ОбщегоНазначения`, `InformationRegister.Цены`,
`AccumulationRegister.Продажи`, `Subsystem.Продажи`, `Role.Администратор`. fqn рутины —
`<fqn-модуля-владельца>::<ИмяМетода>`. Большинство инструментов принимают и fqn, и просто имя объекта.

КАКОЙ ИНСТРУМЕНТ (по потребности):
• Найти по смыслу/ключевым словам → hybrid_search (по умолчанию; также матчит идентификаторы вроде
  'ПродажиТоваров') или semantic_search. Сужайте через kinds / chunk_kinds / subsystem; expand=True
  добавляет окружение из графа к каждому результату. Результаты по коду несут `routine_fqn`
  (передавайте его в find_callers/callees).
• Просмотр/список → list_metadata. Инвентарь и хотспоты → metrics.
• Структура объекта (лёгкая, семантическая) → get_object. Полные сырые свойства конфигурации/UI
  (Hierarchical, Posting, CodeLength, Periodicity, режим блокировки, ...) → get_object_properties или
  get_object(detail=True).
• Связи → get_dependencies / impact_analysis (кто сломается при изменении) / find_type_usages.
• Поведение и код → find_handlers (что выполняется при проведении/записи/проверке + события форм),
  find_callers / find_callees / call_path (граф вызовов BSL).
• Документация → поиск с source=['its'|'artifact'] (ИТС 1С / доки проекта, если загружены);
  find_related_docs (доки, связанные с объектом) / get_document (полный документ по fqn). Результаты
  поиска несут поле 'corpus'; фильтруйте по source, чтобы разделять конфигурацию и документацию.
• СТАНДАРТЫ разработки 1С («как писать по стандартам 1С»: именование/кодирование, обработчики событий,
  правила запросов) → search_standards (ранжированный поиск по корпусу v8std), затем
  get_standard(<номер>) для полного текста одного стандарта. Доступны всегда — стандарты живут в общем
  публичном арендаторе.
• Справка платформы/БСП (синтакс-помощник) → docinfo (точный лукап по имени, напр. 'Массив.Найти' /
  'Array.Find') или поиск с source=['platform_help']. Живут в общем публичном арендаторе и
  ВЕРСИОННО-ЗАВИСИМЫ: передавайте platform_version (напр. '8.3.27.2130'), чтобы зафиксировать сборку —
  используйте его ТОЛЬКО вместе с source=['platform_help'] (он фильтрует по версии документа, иначе
  отсёк бы результаты конфигурации, у которых версии нет). Опустите его, чтобы охватить все загруженные
  версии (docinfo тогда вернёт 'candidates' по версиям, если совпало несколько; единственное совпадение
  возвращает статью). fqn в get_document кодирует версию: 'platform_help:<ver>|<Имя>'.
• Фасеты классификации документов (фильтры поиска по узлу-владельцу — сочетать с соответствующим source,
  иначе конфигурация отсеивается): doc_topic ('platform' | 'config' | 'task') разделяет доки платформы /
  конфигурации / задачи; corpus_version (типизированный, напр. 'config:ERP_2.5.18') фиксирует релиз
  конфигурации; help_kind ('context' | 'language' | 'query') сужает справку синтакс-помощника. Они
  классифицируют; изоляцию по-прежнему определяет арендатор.

СЛОИ ГЛУБИНЫ ДАННЫХ (дёшево → исчерпывающе): поиск и список (обнаружение) → get_object (семантическая
структура) → get_object_properties / :Detail (каждое сырое свойство). Слой деталей намеренно НЕ
участвует в поиске — запрашивайте его по fqn, когда нужны точные факты конфигурации."""

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
@mcp.tool(description="""\
Проверка живости сервера. Возвращает имя и версию сервера.""")
def ping() -> dict[str, Any]:
    """Liveness check. Returns server name and version."""
    return {"status": "ok", "server": "onec-vecgraph", "version": __version__}


@mcp.tool(description="""\
Проверка доступности Neo4j: возвращает редакцию сервера и число узлов.""")
def neo4j_health() -> dict[str, Any]:
    """Check Neo4j connectivity and report server edition and node count."""
    with Neo4jStore.from_settings(settings) as store:
        return store.health()


@mcp.tool(description="""\
Возвращает арендатора/конфигурацию, определённые для этого запроса (проверка прокидывания заголовков).""")
def whoami(ctx: Context) -> dict[str, Any]:
    """Return the tenant/config resolved for this request (to verify header wiring)."""
    scope = tenancy.resolve(ctx, settings)
    return {"tenant_id": scope.tenant_id, "config_id": scope.config_id}


# ── metadata / graph ──────────────────────────────────────────────────
@mcp.tool(description="""\
Список объектов метаданных, опционально с фильтром по виду и подстроке имени/синонима.

kind ∈ Catalog, Document, Enum, InformationRegister, AccumulationRegister, AccountingRegister,
CommonModule, Report, DataProcessor, Constant, ChartOfCharacteristicTypes, Subsystem, Role,
EventSubscription, BusinessProcess, Task, ExchangePlan, ... Возвращает: [{fqn, kind, name, synonym,
config_id}]. Для семантического обнаружения используйте hybrid_search.""")
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


@mcp.tool(description="""\
Полная карточка объекта (по fqn вида 'Catalog.AI_Модели' или по имени): реквизиты с типами,
табличные части, значения перечислений, предопределённые значения, формы, модули, владельцы, подсистемы.

detail=True дополнительно возвращает 'details' — полный сырой набор свойств метаданных (все
<Properties>: Hierarchical, CodeLength, Posting, Periodicity, полнотекстовый поиск, режим блокировки,
стандартные реквизиты, ...) для углублённого разбора разработчиком/аналитиком.""")
def get_object(ctx: Context, query: str, detail: bool = False) -> dict[str, Any]:
    """Full card for an object (by fqn like 'Catalog.AI_Модели' or by name): attributes with types, tabular sections, enum values, predefined values, forms, modules, owners, subsystems.

    detail=True additionally returns 'details' — the full raw metadata property set (every
    <Properties>: Hierarchical, CodeLength, Posting, Periodicity, full-text search, lock mode,
    standard attributes, ...) for developer/analyst deep-dives."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.get_object(store, _tenant(ctx), query, detail=detail)


@mcp.tool(description="""\
Полный сырой набор свойств метаданных объекта (по fqn или имени): все значения <Properties> —
Hierarchical, CodeLength/CodeType, NumberLength, Posting/RealTimePosting, Periodicity, WriteMode,
FullTextSearch, DataLockControlMode, ChoiceMode, стандартные реквизиты и т.д. Для углублённого разбора
разработчиком/аналитиком; хранятся, но намеренно не векторизуются.""")
def get_object_properties(ctx: Context, query: str) -> dict[str, Any]:
    """Full raw metadata property set for an object (by fqn or name): every <Properties> value —
    Hierarchical, CodeLength/CodeType, NumberLength, Posting/RealTimePosting, Periodicity,
    WriteMode, FullTextSearch, DataLockControlMode, ChoiceMode, standard attributes, etc.
    For developer/analyst deep-dives; these are stored but deliberately not vectorized."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.get_object_properties(store, _tenant(ctx), query)


@mcp.tool(description="""\
Граф зависимостей вокруг объекта. direction: 'out' (от чего зависит), 'in' (что зависит от него) или
'both'. Возвращает depends_on/dependents, каждый с 'references' (ссылки по типам) и 'related'
(рёбра CONTAINS/OWNED_BY/SUBSCRIBES/HAS_RIGHT_ON/WRITES_TO/..., помеченные полем 'rel').
overlay_tenant_id ('<base>@task/<id>' в пределах арендатора вызывающего) объединяет
baseline ∪ рабочую копию: исходящие рёбра идут по актуальной версии объекта, входящие сливаются по
владельцу-источнику, tombstone'ы маскируют удаления; строки несут 'layer' (release/working).""")
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


@mcp.tool(description="""\
Что будет затронуто при изменении объекта: входящие ссылки, подсистемы, роли и подписки, зависящие от
него. overlay_tenant_id ('<base>@task/<id>' в пределах арендатора вызывающего) объединяет
baseline ∪ рабочую копию с маскированием через tombstone'ы и пометкой происхождения 'layer' (Phase 2).""")
def impact_analysis(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """What would be affected if this object changes: incoming references, subsystems, roles and
    subscriptions that depend on it. overlay_tenant_id ('<base>@task/<id>' under the caller's tenant)
    unions baseline ∪ working copy with tombstone masking and 'layer' provenance (Phase 2)."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.get_dependencies(store, t, query, "in",
                                        overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool(description="""\
Найти все реквизиты/измерения/ресурсы, использующие данный объект как ссылочный тип.""")
def find_type_usages(ctx: Context, query: str) -> dict[str, Any]:
    """Find all attributes/dimensions/resources that use the given object as their reference type."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_type_usages(store, _tenant(ctx), query)


@mcp.tool(description="""\
Документация (ИТС / артефакты проекта), связанная с объектом (по fqn или имени) через MENTIONS
(явные/сканированные fqn) или RELATES_TO (семантика, с confidence). Отвечает на вопрос «какие
стандарты/доки покрывают этот объект». Возвращает docs:[{fqn, label, source, title, source_url, rel,
confidence}]. Требует, чтобы соответствующий корпус был загружен (см. конвейер `ingest`).""")
def find_related_docs(ctx: Context, query: str) -> dict[str, Any]:
    """Documentation (ITS / project artifacts) linked to an object (by fqn or name) via MENTIONS
    (explicit/scanned fqns) or RELATES_TO (semantic, with confidence). Answers 'what standards/docs
    cover this object'. Returns docs:[{fqn, label, source, title, source_url, rel, confidence}].
    Requires the corresponding corpus to have been ingested (see the `ingest` pipeline)."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_related_docs(store, _tenant(ctx), query)


@mcp.tool(description="""\
Полный документ по fqn владельца ('its:<id>' / 'artifact:<path>#<n>', напр. из поля fqn результата
поиска): метаданные, полный текст (чанки склеены) и объекты конфигурации, на которые он ссылается.
Резолвится в арендаторе вызывающего и в общем публичном арендаторе (справка платформы/БСП).""")
def get_document(ctx: Context, fqn: str) -> dict[str, Any]:
    """Full document by owner fqn ('its:<id>' / 'artifact:<path>#<n>', e.g. from a search hit's fqn):
    metadata, full text (chunks rejoined) and the config objects it links to. Resolves in the
    caller tenant and the shared public tenant (platform/BSP help)."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.get_document(store, t, fqn, shared_tenant_id=_shared(t))


@mcp.tool(description="""\
Точный лукап справки платформы 1С по каноническому имени — синтакс-помощник. Принимает русское имя,
английское имя или точечную форму 'Объект.Метод'/'Объект.Свойство' (напр. 'ТаблицаЗначений',
'Массив.Найти', 'QuerySchema'). Опциональный platform_version (напр. '8.3.27.1989') выбирает сборку;
иначе берётся самая свежая проиндексированная. Одно совпадение → полная статья справки; несколько →
список 'candidates' для уточнения. Читает общий публичный арендатор (без доп. аргументов).""")
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


@mcp.tool(description="""\
Поиск по СТАНДАРТАМ РАЗРАБОТКИ 1С:Предприятие (официальная «Система стандартов и методик разработки
конфигураций», ИТС v8std) по смыслу или ключевым словам: соглашения об именовании/кодировании, правила
обработчиков событий, стандарты запросов, структура модулей, использование общих модулей и т.д.
Используйте всегда, когда нужна рекомендация «как это сделать по стандартам 1С» для написания или ревью
кода конфигурации.

Возвращает ранжированные результаты; каждый несёт fqn стандарта ('its:<id>' — передайте его в
get_standard для полного текста), title, section_path и source_url (its.1c.ru). expand=True добавляет
окружение из графа. Стандарты живут в общем публичном арендаторе и читаются для ЛЮБОГО арендатора —
особый доступ не нужен. (Тонкая обёртка над hybrid_search, привязанная к корпусу стандартов.)""")
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


@mcp.tool(description="""\
Полный текст ОДНОГО стандарта разработки 1С по его номеру или id. Принимает голый номер ('396'), якорь
('std396' / '#std396'), id ('v8std_396') или fqn из результата поиска ('its:v8std_396'). Возвращает
title стандарта, полный текст (чанки склеены), section_path и source_url. Используйте после
search_standards, чтобы прочитать конкретный стандарт целиком.""")
def get_standard(ctx: Context, standard: str) -> dict[str, Any]:
    """Full text of ONE 1C development standard by its number or id. Accepts a bare number ('396'),
    an anchor ('std396' / '#std396'), the id ('v8std_396'), or a search hit's fqn ('its:v8std_396').
    Returns the standard's title, full text (chunks rejoined), section_path and source_url. Use after
    search_standards to read a specific standard end-to-end."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.get_document(store, t, _standard_fqn(standard), shared_tenant_id=_shared(t))


# ── search ────────────────────────────────────────────────────────────
@mcp.tool(description="""\
Семантический (мульти-вектор) поиск по проиндексированным корпусам по смыслу.

Опциональные фильтры: source (корпус: ['config','its','artifact','platform_help','bsp_help'] — config =
конфигурация 1С, its = доки ИТС 1С, artifact = доки проекта, platform_help/bsp_help = публичная справка
платформы/библиотеки из общего арендатора), platform_version (напр. '8.3.27.1989' — ограничить справку
одной сборкой), kinds (виды объектов вроде ['Catalog','Document','Subsystem']), chunk_kinds
(['object','attribute','code','form','enum_value','predefined','subsystem','role']), subsystem
(имя/fqn). Фасеты классификации документов (узел-владелец — сочетать с соответствующим source, иначе
config отсеивается): doc_topic ('platform' | 'config' | 'task'), corpus_version (типизированный, напр.
'config:ERP_2.5.18' / 'task:JIRA-1234'), help_kind ('context' | 'language' | 'query'). Публичные корпуса
читаются аддитивно из общего арендатора — без доп. аргументов. Результаты по коду возвращают
гранулярность рутины ('routine_fqn'); каждый результат несёт 'corpus'. expand=True добавляет компактное
окружение из графа ('context'). Требует предварительной векторизации.""")
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


@mcp.tool(description="""\
Гибридный поиск (мульти-вектор + полнотекст + RRF). Лучший для смешанных запросов «смысл + идентификатор».

Те же опциональные фильтры, что у semantic_search (source / platform_version / kinds / chunk_kinds /
subsystem / doc_topic / corpus_version / help_kind / expand). source выбирает корпуса
(['config','its','artifact','platform_help','bsp_help']); публичные корпуса читаются аддитивно из общего
арендатора. platform_version ограничивает справку одной сборкой; doc_topic/corpus_version/help_kind —
фасеты узла-владельца (сочетать с соответствующим source). Идентификаторы токенизируются на под-слова,
поэтому 'Продажи' матчит 'ПродажиТоваров'. Результаты по коду имеют гранулярность рутины; каждый несёт
'corpus'. Форма результата та же, что у semantic_search.""")
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


@mcp.tool(description="""\
Метрики инвентаря и хотспотов: количество объектов по видам, объём кода, рёбра графа вызовов по
kind/confidence, хотспоты fan-in/out, точки входа поведения. Опционально ограничивается подсистемой.""")
def metrics(ctx: Context, subsystem: str | None = None) -> dict[str, Any]:
    """Inventory & hotspot metrics: object counts by kind, code volume, call-graph edges by
    kind/confidence, fan-in/out hotspots, behavior entry points. Optionally scoped to a subsystem."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.metrics(store, _tenant(ctx), subsystem)


# ── BSL call graph ────────────────────────────────────────────────────
@mcp.tool(description="""\
Какие BSL-рутины вызывают данную процедуру/функцию (по fqn рутины, 'Модуль.Метод' или голому имени).
Возвращает {query, routines, callers:[{fqn, name, object, kind, confidence}], count}. overlay_tenant_id
объединяет baseline ∪ рабочую копию (caller'ы помечены 'layer'). Требует граф вызовов.""")
def find_callers(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Which BSL routines call the given procedure/function (by routine fqn, 'Module.Method', or
    bare name). Returns {query, routines, callers:[{fqn, name, object, kind, confidence}], count}.
    overlay_tenant_id unions baseline ∪ working copy (callers tagged 'layer'). Requires the call graph."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.find_callers(store, t, query, overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool(description="""\
Какие BSL-рутины вызывает данная процедура/функция. Возвращает {query, routines,
callees:[{fqn, name, object, kind, via (local/common_module/manager)}], count}. overlay_tenant_id
объединяет baseline ∪ рабочую копию (callee'ы помечены 'layer'). Требует граф вызовов.""")
def find_callees(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Which BSL routines the given procedure/function calls. Returns {query, routines,
    callees:[{fqn, name, object, kind, via (local/common_module/manager)}], count}.
    overlay_tenant_id unions baseline ∪ working copy (callees tagged 'layer'). Requires the call graph."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.find_callees(store, t, query, overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool(description="""\
Объединённый граф вызовов BSL вокруг рутины: {callers, callees}. С overlay_tenant_id
('<base>@task/<id>' в пределах арендатора вызывающего) результат объединяет baseline ∪ рабочую копию
(overlay выигрывает, tombstone'ы маскируют удаления; строки несут 'layer'). Требует граф вызовов.""")
def call_graph(ctx: Context, query: str, overlay_tenant_id: str | None = None) -> dict[str, Any]:
    """Combined BSL call graph around a routine: {callers, callees}. With overlay_tenant_id
    ('<base>@task/<id>' under the caller's tenant) the result unions baseline ∪ working copy
    (overlay wins, tombstones mask deletions; rows carry 'layer'). Requires the call graph."""
    with Neo4jStore.from_settings(settings) as store:
        t = _tenant(ctx)
        return queries.call_graph(store, t, query, overlay_tenant_id=_overlay(t, overlay_tenant_id))


@mcp.tool(description="""\
Кратчайший путь вызовов BSL между двумя рутинами (один арендатор; для overlay-аналитики используйте
call_graph — кросс-слойный поиск пути не объединяется, см. docs/OVERLAY.md).""")
def call_path(ctx: Context, from_routine: str, to_routine: str) -> dict[str, Any]:
    """Shortest BSL call path between two routines (single tenant; for overlay-aware analysis use
    call_graph — cross-layer path-finding is not unioned, see docs/OVERLAY.md)."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.call_path(store, _tenant(ctx), from_routine, to_routine)


@mcp.tool(description="""\
Точки входа поведения объекта (по fqn или имени): обработчики событий форм (событие→рутина, через
HANDLES) и стандартные события модулей (проведение/запись/проверка_заполнения/нумерация/…). Отвечает на
«что выполняется при проведении/записи/проверке» и «какие события форм есть».""")
def find_handlers(ctx: Context, query: str) -> dict[str, Any]:
    """Behavior entry points of an object (by fqn or name): form event handlers (event→routine,
    via HANDLES) and standard module events (проведение/запись/проверка_заполнения/нумерация/…).
    Answers 'what runs when this is posted/written/validated' and 'which form events exist'."""
    with Neo4jStore.from_settings(settings) as store:
        return queries.find_handlers(store, _tenant(ctx), query)


def run(transport: str = "streamable-http") -> None:
    mcp.run(transport=transport)
