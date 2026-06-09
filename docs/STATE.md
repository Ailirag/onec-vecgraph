# Состояние проекта onec-vecgraph (хендофф для новой сессии)

> Дата фиксации: 2026-06-08. Читать ПЕРВЫМ при старте новой сессии.
> Авто-память (`MEMORY.md` и связанные файлы) загружается автоматически; этот файл — полный снимок.
> Связанное: `README.md`, `PLAN.md`, `docs/INCREMENTAL_TEST_PLAN.md`, `docs/DEPLOYMENT.md`,
> `docs/DEPLOY_RUNBOOK.md`, `docs/MCP_USAGE.md`, `docs/ITS_PARSER_REQUIREMENTS.md` (контракт парсера ИТС).

> **Git:** репозиторий `https://github.com/Ailirag/onec-vecgraph` (private), ветка `main`.
> Флоу: `git add -A; git commit -m "…"; git push`. `.gitignore` исключает `.env`, `.venv/`, `data/`,
> `snapshots/`, `scripts/*.out.json` — секреты и тяжёлые артефакты не коммитим. Коммитить/пушить ТОЛЬКО
> по явной просьбе пользователя. Из PowerShell push работает (egress к github:443 открыт, helper=GCM).

## 1. Что это

MCP-сервер: **векторизация конфигураций 1С (из XML-выгрузки Конфигуратора, НЕ EDT) + граф
зависимостей + граф вызовов BSL**, всё в **Neo4j** (граф и векторы в одном хранилище).
Назначение — сетевой **мультиарендный** компонент большой SDLC-системы (БА→СА→архитектор→
разработчик→ревьюер→тимлид). Ниша подтверждена: единственное решение, делающее всё это строго из
формата Конфигуратора (см. PLAN.md, анализ аналогов FSerg/metacode/bsl-graph/documents1c).

## 2. Окружение (КРИТИЧНО для возобновления — всё на диске D, Windows + PowerShell)

- **Python 3.12** через **uv** (uv установлен в `D:\tools\uv`, не в PATH свежей сессии).
  В КАЖДОЙ PowerShell-команде префиксить: `$env:Path="D:\tools\uv;$env:Path"`.
- Кеш uv: `D:/tools/uv/cache` (в `[tool.uv]` pyproject); managed-Python: `D:\tools\uv\python`
  (env `UV_PYTHON_INSTALL_DIR`, закреплён в User-env); `.venv` в проекте.
- **torch 2.11.0+cu128** (RTX 5080, Blackwell sm_120) — ставится через extra `local-embeddings`:
  `uv sync --extra local-embeddings`. Индекс cu128 в `pyproject [[tool.uv.index]]` + `[tool.uv.sources] torch`.
  CUDA проверена: `torch.cuda.is_available()=True`.
- **HF-кеш моделей**: `HF_HOME=D:\tools\hf-cache` (env). Первый запуск модели качает ~1.2 ГБ.
- **Кириллица в консоли**: ставить `[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=...UTF8; $env:PYTHONUTF8='1'`.
  Даже так возможен мусор cp1251 в выводе — это ТОЛЬКО отображение, в Neo4j/JSON данные корректны.
  Для проверки кириллицы писать результат в JSON-файл и читать его (Read-tool декодирует UTF-8).
- **Neo4j 5.26-community** через `docker compose up -d --wait`; данные `./data/neo4j` (диск D).
  Память (в docker-compose.yml): heap **8G**, pagecache **4G**, `dbms.memory.transaction.total.max=6G`,
  `db.memory.transaction.max=4G`. Логин neo4j/onec_vecgraph_dev (в `.env`). При смене env compose
  нужно `docker compose up -d --force-recreate` (обычный up не пересоздаёт).
- **.env**: `EMBEDDING_PROVIDER=local`, `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B`,
  `EMBEDDING_DEVICE=auto`, `EMBEDDING_BATCH_SIZE=16` (на масштабе переопределять env-ом 32–64),
  `EMBEDDING_MAX_SEQ_LENGTH=256`, `REQUIRE_TENANT=true`. Дефолт-модель 0.6B (1024-dim); для качества —
  `Qwen/Qwen3-Embedding-4B` (2560).

## 3. Тестовые выгрузки (на диске H:)

- `H:\1C\xml\LLM_Subsystem_test` — малая (база `cf` + расширение `cfe llm`, ~72 объекта). Tenant `demo`.
- `H:\1C\xml\ERP UH` — полная ERP/УХ (~24k объектов, 88k XML). Tenant `erp_test` (и старый `erp`).
  Перевыгружена пользователем 2026-06-08 (включена возможность изменения + 13 точечных правок — см. п.9).
- `H:\Гранд трейд\gitlab\ones\ut_configurator_xml` — УТ (ConfigDumpInfo сейчас отсутствует). Tenant `ut` (старый).
- Слепки хешей: `snapshots/erp_baseline_2026-06-05.json`, `snapshots/erp_after_2026-06-08.json`.

## 4. Состояние данных в Neo4j (что в каком tenant)

- **`demo` — ПОЛНОСТЬЮ актуален и со ВСЕМИ фичами (вкл. расширение под роли п.1–10, 2026-06-08)**:
  граф метаданных (+WRITES_TO), векторы **с `--code`** (~1.6k чанков: object/attribute/tabular/enum/
  predefined/form/code(cAST, 282 части)/subsystem(3)/role(4)), граф вызовов **с модулями форм + HANDLES +
  менеджерными вызовами + entry_point** (manager calls 21), **`:Detail` 70 узлов** (полные `<Properties>`).
  В demo НЕТ документов → WRITES_TO/объектные точки входа (проведение/запись) пусты (проверены на ERP).
- **`erp_test` — реиндексирован 2026-06-08 под фичи п.1–10**: граф (24k объектов, form_path есть,
  **WRITES_TO 8152**), callgraph (181k рутин модулей + 437k рутин форм, calls: local 562k/common 567k/
  **manager 26117**/unresolved 1.6M, **HANDLES 95468**, entry_points: проведение 1334/запись 3225/
  заполнение 1765/проверка_заполнения 1201/…), **векторы МЕТАДАННЫХ** (146 964 чанка вкл. subsystem 933/
  role 1847; **code-чанков НЕТ** — `vectorize --code` не прогонялся, это ~часы), **`:Detail` 23 998 узлов**.
  Поиск по коду на ERP не валидирован (валидирован на demo); граф-фичи, поиск по метаданным и слой Detail
  валидированы на ERP. Чтобы добить code: `vectorize --tenant-id erp_test --code` (батч 64; ~часы).
- **`erp`, `ut`** — старые, без config_version/квалиф.fqn — устаревшие, можно удалить (`store.delete_tenant`).

## 5. Что готово (этапы)

- **M0** каркас (config, tenancy, FastMCP HTTP+stdio, Neo4j).
- **M1/M1.1** парсер метаданных + граф (объекты, реквизиты/типы, ТЧ, перечисления, предопределённые,
  владельцы, подсистемы, права ролей, подписки, формы(имена), модули(пути)). Все виды объектов в TYPE_FOLDERS.
- **M2/M2.1** векторизация: мульти-вектор (имя×смысл) + полнотекст, RRF; чанки object/attribute/
  tabular_attribute/enum_value/predefined; опц. реранкер (выкл). Qwen3-Embedding/CUDA.
- **M3** граф вызовов BSL (чистый Python-парсер `bsl/parser.py`): Routine, DECLARES, CALLS (local +
  общие модули, high-precision; неразрешённые не пишем). `find_callers/callees/call_path`.
- **HTTP-мультиарендность**: tenant/config из заголовков `X-Tenant-Id`/`X-Config-Id` (`tenancy.resolve`),
  `stateless_http`, `require_tenant` (без заголовка по HTTP — отказ). `whoami`.
- **Инкрементальность** (по `configVersion` из ConfigDumpInfo): `index|vectorize|callgraph --incremental`;
  scoped-перестройка изменённых с сохранением входящих рёбер; удаление исчезнувших. Идемпотентно.
- **Квалиф. fqn подсистем** (`Subsystem.Parent.Subsystem.Child`) — совпадают с ConfigDumpInfo.
- **Векторизация форм** (UI: заголовки/подписи из Form.xml) — chunk_kind `form`.
- **Код модулей форм** (2026-06-08): директивы в парсере; `parse_form_handlers`; code-чанки
  (`vectorize --code`); модули форм в графе вызовов + рёбра **HANDLES** (событие формы → обработчик).
- **Расширение под роли (пункты 1–10, 2026-06-08)** — универсальные возможности MCP (без хардкода ролей):
  1. **Фильтры поиска** `kinds`/`chunk_kinds`/`subsystem` в `semantic_search`/`hybrid_search` (+CLI `--kind/--chunk-kind/--subsystem`).
     Для редких фильтров — **точный косинус** (`vector.similarity.cosine`) по кандидатам (нулевая потеря recall),
     при множестве > `_EXACT_SCAN_CAP=50000` падение на vector-index+пост-фильтр (`queries._vector_retrievers`).
  2. **Гранулярность кода**: результаты code-чанков возвращают `routine_fqn` (адрес рутины), части `#code/N`
     схлопываются к рутине (`queries._unit`/`_routine_fqn`). Дедуп = `_dedup` (по unit, не по объекту).
  3. **Точки входа**: `chunking.classify_entry_point` (проведение/запись/проверка_заполнения/нумерация/выбор/
     событие_формы) → `Chunk.entry_point` + `Routine.entry_point`; инструмент **`find_handlers`** (формы+модули).
  4. **FTS по идентификаторам**: `chunking.search_tokens` (split CamelCase/ВерхнийРегистр/точка) → поле
     `Chunk.text_tokens`, fulltext-индекс `chunk_text` по `[text, text_tokens]`; запрос токенизируется (`_fts_query`).
  5. **GraphRAG-достройка**: `search ... expand=True` → `context` (объект: реквизиты/подсистемы/ссылки/движения;
     код: object/entry_point/callers/callees) — `queries._expand`.
  6. **cAST-чанкинг кода**: `chunking.code_chunks` (split-then-merge по бюджету не-проб. символов
     `_CODE_BUDGET_NONWS=1200`, без обрезки; мелкие точки входа сохраняются).
  7. **Подсистемы и Роли как чанки**: `subsystem_chunk` (состав), `role_chunk` (права на объекты) —
     chunk_kind `subsystem`/`role`; исключены из общей object-карточки.
  8. **`WRITES_TO`** (Документ→Регистр, из `<RegisterRecords>`): парсер+билдер; в `get_dependencies` (`_RELATED_RELS`).
  9. **Метрики**: инструмент **`metrics`** (объекты по видам, рутины, code_bytes, calls по kind/confidence,
     entry_points, fan-in/out хотспоты; опц. scope=подсистема).
  10. **Менеджерные вызовы** `Справочники.X.Метод()`/`Документы.X…` → ManagerModule (confidence **medium**,
      kind `manager`); индекс из графа для инкремента (`store.manager_module_routine_index`).
- **Слой детализации `:Detail` (2026-06-08)** — полный набор `<Properties>` объекта (≈40–50 конфиг/UI-свойств:
  Hierarchical, CodeLength, Posting, Periodicity, FullTextSearch, DataLockControlMode, ChoiceMode, EditType…)
  в **сайдкар-узле `:Detail`** (ключ `(tenant_id, fqn)`, ребро `HAS_DETAIL` Object→Detail). **НЕ векторизуется**
  (поиск не загрязняется), отдаётся по запросу: `get_object_properties` / `get_object(detail=True)` / CLI `show --detail`.
  `parsing/objects._flatten_properties`: скаляры→текст, структурные (StandardAttributes/InputByString/Characteristics/
  Synonym/Owners)→**raw-XML с обрезкой `_RAW_MAX=2000`** (осознанно; нормальный разбор структурных — TODO).
  Создаётся при `index` (в т.ч. **неразрушающем без `--reset`** — MERGE поверх, векторы/рутины целы) и в инкременте.
- **Упаковка/деплой (2026-06-08)**: **bearer-аутентификация** (`AUTH_ENABLED`/`AUTH_TOKENS`, токен→tenant,
  игнорирует подделанный `X-Tenant-Id`; opt-in, легаси-режим доверенного заголовка сохранён —
  `tenancy.resolve`); **облачные эмбеддинги** `embeddings/cloud.py` (OpenAI/OpenAI-совместимые + Voyage);
  **Dockerfile** с build-args `EXTRAS` (local|cloud) и `TORCH_INDEX_URL` (cpu|cu128) → 3 варианта образа;
  app-сервис в `docker-compose.yml`; гайды `docs/MCP_USAGE.md` (консьюмер), `docs/DEPLOYMENT.md` (деплой),
  `docs/DEPLOY_RUNBOOK.md` (пошаговый runbook). Образ конфигурация-агностичен: выгрузка — runtime-том
  (`-v …:/dump`), несколько конфигураций = отдельные `index --tenant-id …`, один сервер маршрутизирует по tenant.
- **Аутентификация + Docker (2026-06-08)**: bearer-токен→tenant (`AUTH_ENABLED`/`AUTH_TOKENS` в `config`,
  логика в `tenancy.resolve`/`_bearer_token`; токен закрепляет tenant[:config], `X-Tenant-Id` тогда
  игнорируется как недоверенный). По умолчанию `AUTH_ENABLED=false` → легаси доверенный `X-Tenant-Id`
  (dev/stdio, тесты не ломаются). **`Dockerfile`** (build-arg `TORCH_INDEX_URL` — CPU по умолчанию /
  cu128 для GPU) + **app-сервис в `docker-compose.yml`** (env-конфиг, том `./data/hf-cache` под модель,
  healthcheck `onec-vecgraph health`). `.dockerignore`, `.env.example` (AUTH_*). Сервер эмбеддит только
  запрос → на CPU достаточно; тяжёлая индексация/векторизация — офлайн (CLI, GPU-хост). **NB: образ в этой
  сессии не собирался** (нужен интернет для pip: torch/transformers); compose-конфиг валиден (`compose config`).
  Consumer-overview сервер отдаёт клиенту в `instructions` (`server.INSTRUCTIONS`); гайд — `docs/MCP_USAGE.md`.
- **Мультиисточник векторизации (2026-06-08, РЕАЛИЗОВАН п.1–7)**: измерение `source` (config/its/artifact)
  сквозь чанк/поиск/MCP/CLI; узлы-владельцы `:Document`/`:Artifact` (обобщён `write_chunks(owner_label=…)`);
  пакет `sources/` (контракт `Source`/`DocUnit`, реестр, YAML/JSON-манифест, адаптеры `its` и `git_artifacts`,
  Markdown-сплиттер, линковка); драйвер `ingest.py` (инкремент по `version_hash`; `config_dump`→index+callgraph+
  vectorize); рёбра `MENTIONS` (явные/сканированные fqn) + `RELATES_TO` (семантика, opt-in `--link-semantic`);
  инструменты `find_related_docs`/`get_document`; doc-`expand`. Все источники — подключаемые git-репо (клон
  системным `git`) или локальный `path` (для тестов/офлайна). Проверено e2e на demo (its+artifact: ингест,
  инкремент идемпотентен, фильтр `source`, MENTIONS/RELATES_TO, поиск по корпусам не протекает в config).

## 6. Модель графа (Neo4j)

- **Ключ узла**: `(tenant_id, fqn)` MERGE. Constraints + индексы — `graph/schema.py`.
- **Узлы (labels)**: `Object` (вид в property `kind`), `Field` (role), `TabularSection`, `EnumValue`,
  `Predefined`, `Form`, `Module`, `Routine`, `Chunk`, `Detail` (полный набор `<Properties>`, не векторизуется),
  `Document`/`Artifact` (владельцы doc-чанков корпусов ИТС/артефактов; ключ `(tenant_id, fqn)`, fqn=`<source>:<id>`).
- **Рёбра**: `CONTAINS` (подсистема→объект), `HAS_ATTRIBUTE/HAS_DIMENSION/HAS_RESOURCE`,
  `HAS_TABULAR_SECTION`, `HAS_ENUM_VALUE`, `HAS_PREDEFINED`, `HAS_FORM`, `HAS_MODULE`,
  `REFERENCES` (Field→Object, по ссылочному типу), `OWNED_BY`, `HAS_SUBSYSTEM`, `SUBSCRIBES`,
  `HANDLED_BY` (подписка→общий модуль), `HAS_RIGHT_ON` (роль→объект, схлопнуто до верхнего уровня),
  `DECLARES` (Module/Form→Routine), `CALLS` (Routine→Routine, confidence/kind; kind=local/common_module/
  **manager**), `HANDLES` (Form→Routine, event/element), `WRITES_TO` (Документ→Регистр),
  `HAS_CHUNK` (Object|Document|Artifact→Chunk), `HAS_DETAIL` (Object→Detail, жёсткое),
  `MENTIONS` (Document/Artifact→Object, явные/сканированные fqn), `RELATES_TO` (Document/Artifact→Object,
  семантика, `confidence`).
- **chunk_kind**: object, attribute, tabular_attribute, enum_value, predefined, form, code, **subsystem**,
  **role**, **its**, **artifact**. Code-чанки дробятся (`…::name#code/N`); doc-чанки (`<owner>#chunk[/N]`);
  несут `entry_point` (code) и **`source`** (config|its|artifact). Routine-узлы несут `entry_point`.
- **Индексы поиска**: vector `chunk_embedding` (semantic) + `chunk_embedding_ident` (ident), cosine,
  dim из модели; fulltext `chunk_text` по `[text, text_tokens]` (идентификаторы расщеплены на под-слова).
  Поиск = мульти-вектор × fulltext, слияние RRF (+опц. rerank); фильтры **source**/kinds/chunk_kinds/subsystem;
  опц. expand. Владелец чанка в поиске — любой (`MATCH (o)-[:HAS_CHUNK]->(c)`), не только Object.
- Висячие/внешние ссылки → стаб `:Object{stub:true, kind:'Unresolved'|...}`; counts() их исключает.

## 7. Карта кода (`src/onec_vecgraph/`)

- `config.py` — Settings (pydantic-settings, .env).
- `tenancy.py` — TenantContext, `resolve(ctx, settings)` (заголовки→tenant, require_tenant).
- `cli.py` — Typer: version, health, serve, index, ls, show (+`--detail`), deps, usages, vectorize, search
  (+`--kind/--chunk-kind/--subsystem/--source/--expand`), **handlers**, **metrics**, **ingest**, callgraph,
  callers, callees, path, snapshot, snapshot-diff. `_flush_exit()`=os._exit (см. гочи).
- `server.py` — FastMCP, 18 инструментов (см. п.8), stateless_http.
- `indexer.py` — `index_dump(..., reset, incremental)` (полный/инкремент).
- `vectorizer.py` — `vectorize(..., reset, incremental, code)`; `_iter_chunks` (+subsystem/role циклы)/`_iter_code_chunks`.
- `callgrapher.py` — `build_call_graph(...)`; `_parse_modules` (+manager_index)/`_parse_form_modules`/`_resolve`
  (+manager, entry_point)/`_full`.
- `chunking.py` — Chunk(+entry_point) + builders (object/attribute/tabular/enum/predefined/form/**code_chunks**
  cAST/**subsystem**/**role**), `search_tokens`, `classify_entry_point`, `_split_code`, KIND_RU.
- `queries.py` — list_metadata, get_object (+`detail`), **get_object_properties**/`_object_details` (из `:Detail`),
  get_dependencies (+WRITES_TO), impact_analysis, find_type_usages, semantic_search/hybrid_search (фильтры+expand;
  `_vector_retrievers` exact/index, `_dedup`/`_unit`/`_rrf_fuse`, `_fts_query`, `_expand`, `_rerank`),
  find_callers/callees, **find_handlers**, call_path, **metrics**, **find_related_docs**/**get_document** (doc-корпуса),
  `semantic_search`/`hybrid_search` (+`source`-фильтр; `_expand` имеет doc-ветку с `links`).
- `sources/` — мультиисточник: `base` (`Source` ABC, `DocUnit`, `owner_fqn`, `sha1_text`), `git_repo`
  (`materialize` — локальный path / `git clone --depth 1` системным git; `iter_files`), `markdown`
  (`split_markdown_sections`), `its` (`ItsSource`), `git_artifacts` (`GitArtifactsSource`), `linking`
  (`extract_fqn_mentions`/`link_mentions`), `registry` (`build_source`), `manifest` (`load_manifest` YAML/JSON).
- `ingest.py` — `ingest_source` (doc-корпус: инкремент по version_hash → owners+chunks+embed+MENTIONS+опц.RELATES_TO),
  `ingest_manifest` (по манифесту; `config_dump`→index+callgraph+vectorize), `_link_semantic`.
- `parsing/`: `ns` (namespaces+хелперы), `types` (разбор типов, cfg-ссылки, alias ConstantValue→Constant),
  `model` (dataclasses; +`MetaObject.register_records`/`details`), `objects` (разбор MetaDataObject +
  rights/predefined; +`_flatten_properties`→`obj.details`; +RegisterRecords для Document), `dump` (части/обход/
  enumerate_objects/parse_objects), `dumpinfo` (configVersion), `forms` (extract_form_text, parse_form_handlers).
- `graph/`: `schema` (constraints/indexes; +`Detail`/`Document`/`Artifact` в NODE_LABELS), `builder`
  (модель→узлы/рёбра, group MERGE; +узел `Detail`/ребро `HAS_DETAIL`, +`WRITES_TO`).
- `storage/neo4j_store.py` — драйвер: health, read/write, ensure_schema, write_graph, counts,
  delete_tenant (батч), incremental (object_versions, scoped_delete_object, delete_object_full),
  vectors (**write_chunks(owner_label=Object|Document|Artifact)** 2 вектора+text_tokens,
  create_vector_index/fulltext[text,text_tokens], stale_chunk_owners, delete_chunks_for, vector_search/
  fulltext_search/**exact_vector_search**/**filtered_chunk_count** — фильтры **source**/kinds/chunk_kinds/subsystem,
  владелец любой), doc-корпуса (**write_documents**, **doc_versions**, **delete_docs**, **delete_source**,
  **existing_object_fqns**, **write_mentions**, **write_relates**), callgraph (routine_modules, write_routines,
  write_calls, stale_routine_owners, delete_routines_for, common_module_routine_index,
  **manager_module_routine_index**, form_modules, write_form_routines, write_handles). `notifications_min_severity="OFF"`.
- `embeddings/`: base (get_provider), hashing (dev, без ML), local (sentence-transformers, Qwen3, cuda,
  max_seq_length), **cloud** (OpenAI/OpenAI-совместимые + Voyage; `CloudEmbeddingProvider`, L2-норм,
  dim из карты/override/probe; `--extra cloud-embeddings`), reranker (CrossEncoder, опц.),
  runtime (кэш провайдера/реранкера).
- `bsl/parser.py` — чистый Python BSL-парсер: процедуры/функции, export, region, **directive**, вызовы.

## 8. MCP-инструменты (18)

> Консьюмер-гайд (подключение/заголовки/fqn/словари/карта инструментов/сценарии): `docs/MCP_USAGE.md`.
> Сервер отдаёт тот же overview клиенту в `instructions` (FastMCP) при `initialize` — `server.INSTRUCTIONS`.

ping, neo4j_health, whoami, list_metadata, get_object (+`detail`), **get_object_properties**
(полный сырой набор `<Properties>` из `:Detail`), get_dependencies, impact_analysis,
find_type_usages, **find_related_docs** (доки по объекту), **get_document** (документ по fqn),
semantic_search, hybrid_search, **metrics** (инвентарь/хотспоты),
find_callers, find_callees, call_path, **find_handlers** (обработчики форм+модулей).
`semantic_search`/`hybrid_search` принимают `source`/`kinds`/`chunk_kinds`/`subsystem`/`expand`.

## 9. Ключевые решённые вопросы / находки

- **Neo4j как единое хранилище** векторов(HNSW)+графа — подтверждено (GraphRAG в одном Cypher).
- **T-CODE-OBJ (валидация на реальных правках ERP)**: `configVersion` ОБЪЕКТА **меняется** при правке
  кода его объектного/менеджерского/общего модуля (в ConfigDumpInfo меняются и `Catalog.X`, и
  `Catalog.X.ObjectModule`/`.ManagerModule`). → инкрементальный callgraph по configVersion корректен
  для них. **Модуль формы** версионируется отдельно (`...Form.X`), объект НЕ меняется.
- ConfigDumpInfo содержит суб-записи `.ObjectModule/.ManagerModule/.Module/.Form/.Predefined/.Rights`.
- Векторизация ERP на 0.6B: ~19 мин (батч 64, мульти-вектор), граф метаданных ~80 c, граф вызовов ~4.5 мин.

## 10. Известные ограничения / TODO (приоритет)

1. **CommonForm (общие формы)** векторизуются только карточкой-объектом; их UI/код как форма НЕ парсятся
   (формы ОБЪЕКТОВ — да). В ERP ~493 общих форм.
2. ~~Менеджерные вызовы → unresolved~~ **РЕШЕНО (п.10)**: `Справочники.X.Метод`/`Документы.X…` резолвятся
   к ManagerModule (confidence medium). Ограничение: regex берёт только имя объекта (префикс коллекции
   `Справочники./Документы.` теряется) → при коллизии имён между видами берётся последний (last-wins).
3. **Инкремент форм по версии формы**: code/form/callgraph-данные форм версионируются по ВЛАДЕЛЬЦУ →
   правка только формы не обновится инкрементально (нужна версия формы на узлах Form/Routine).
4. **Инкрементальный callgraph для модулей форм** не реализован (форм-рутины только в полном `callgraph`).
   Аналогично: `entry_point`/`text_tokens`/cAST-части перестраиваются только при полной векторизации/callgraph.
5. **Заимствованные дубли base/ext** (один fqn в базе и расширении) «дрожат» при инкременте (для
   одно-базовых УТ/ERP неактуально).
6. ~~Облачные эмбеддинги не реализованы~~ **РЕШЕНО**: `embeddings/cloud.py` (OpenAI/OpenAI-совместимые
   + Voyage, `--extra cloud-embeddings`). Важно: модель/провайдер должны совпадать при `vectorize` и
   при запросах (dim векторного индекса фиксируется при векторизации); смешивать на одном tenant нельзя.
7. tree-sitter не используется (намеренно — портируемость); парсер эвристический.
8. ~~MCP-инструмент по HANDLES не выведен~~ **РЕШЕНО (п.3)**: инструмент `find_handlers` (формы+модули).
9. **`metrics` доля unresolved-вызовов** не считается из графа (неразрешённые рёбра не пишутся) — только
   распределение разрешённых по kind/confidence.
10. **Аутентификация — opt-in**: по умолчанию `AUTH_ENABLED=false` (доверенный `X-Tenant-Id`). Для сетевого
    выката ОБЯЗАТЕЛЬНО включить bearer (`AUTH_ENABLED=true`+`AUTH_TOKENS`) либо ставить за аутентиф. gateway.
    Токены — плоский env-список (не ротация/не БД секретов); для прод-масштаба возможен внешний secret-store.
11. **Docker-образ в сессии не собран** (pip-сеть). GPU-в-контейнере требует NVIDIA Container Toolkit +
    cu128 build-arg + раскомментировать `deploy.resources`. Первый запрос качает модель (~1.2 ГБ) в том.

## 11. Ожидающие задачи / backlog

### 11.1. Мульти-источник векторизации (config + ИТС + git-артефакты) — ✅ РЕАЛИЗОВАН (п.1–7, 2026-06-08)
Единый граф/индекс/модель; doc-корпуса связаны с объектами (`MENTIONS`/`RELATES_TO`) → GraphRAG.
Детали кода — §5/§6/§7. Парсинг ИТС — внешний инструмент, контракт `docs/ITS_PARSER_REQUIREMENTS.md`.
**Использование:** `ingest <manifest.yaml> --tenant-id <t> [--only its|git_artifacts|config_dump] [--reset]
[--link-semantic]`; или программно `sources.*` + `ingest.ingest_source`. Манифест YAML/JSON: `{tenant, sources:
[{type, repo|path, branch?, globs?}]}`. `git_artifacts`/`its` — git-репо (`repo`) или локальный `path` (офлайн/тесты).

**Открытые TODO по фиче (на будущее):**
- ERP/реальные корпуса не заливались (нет данных ИТС/репозитория артефактов) — проверено только на синтетике demo.
- `RELATES_TO` (семантика) — opt-in `--link-semantic`, по одному vector-запросу на документ (на больших корпусах
  дорого) — при масштабе можно батчить/ограничивать.
- `_expand` для doc-хитов отдаёт только `links`; не достраивает текст связанных объектов.
- Срез ИТС по подсистемам проекта (анти-шум) — не реализован (адаптер берёт всё, что отдал парсер).
- Образ с git/pyyaml в сессии не собирался (pip-сеть); юнит-тесты офлайн + e2e на demo пройдены.

### 11.2. Раунд 2 инкремент-тестов
`docs/INCREMENTAL_TEST_PLAN.md` — РАУНД 1 (13 кейсов) ВЫПОЛНЕН и проверен (см. п.9, T-CODE-OBJ ✓,
инкремент идемпотентен после фиксов). РАУНД 2 (added/deleted/rename — кейсы 15–17) НЕ проводился.

## 12. Базовые команды (PowerShell, с префиксом PATH)

```powershell
$env:Path="D:\tools\uv;$env:Path"; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:PYTHONUTF8='1'; $env:HF_HOME='D:\tools\hf-cache'
docker compose up -d --wait                                                  # Neo4j
uv sync                                                                       # базовые зависимости
uv sync --extra local-embeddings                                             # + torch cu128 (для векторов)
uv sync --extra ingest                                                       # + pyyaml (манифесты источников)
uv run pytest tests/                                                          # тесты (46; bare `pytest` собирает 0 — см. гочи)
uv run onec-vecgraph index "H:\1C\xml\LLM_Subsystem_test" --tenant-id demo --reset
uv run onec-vecgraph index "<путь>" --tenant-id <t> --incremental
uv run onec-vecgraph vectorize --tenant-id demo                              # метаданные (+формы/подсистемы/роли)
uv run onec-vecgraph vectorize --tenant-id demo --code                       # + код модулей (cAST)
uv run onec-vecgraph vectorize --tenant-id demo --incremental
uv run onec-vecgraph callgraph --tenant-id demo                              # граф вызовов (+формы/HANDLES/manager/entry_point)
uv run onec-vecgraph search "запрос" --tenant-id demo --mode hybrid          # +фильтры: --kind/--chunk-kind/--subsystem/--expand
uv run onec-vecgraph search "Провести" --tenant-id demo --chunk-kind code --expand
uv run onec-vecgraph handlers Document.Имя --tenant-id demo                  # обработчики форм+модулей
uv run onec-vecgraph metrics --tenant-id demo [--subsystem Имя]              # инвентарь/хотспоты
uv run onec-vecgraph ingest sources.yaml --tenant-id demo [--only its|git_artifacts|config_dump] [--reset] [--link-semantic]
uv run onec-vecgraph search "проведение" --tenant-id demo --source its --source artifact  # поиск по корпусам доков
uv run onec-vecgraph show Catalog.Имя --tenant-id demo [--detail]            # --detail = + полные <Properties> из :Detail
uv run onec-vecgraph index "<путь>" --tenant-id <t>                          # БЕЗ флагов = неразрушающий MERGE (добавить :Detail, не стирая векторы)
uv run onec-vecgraph deps Catalog.Имя --tenant-id demo
uv run onec-vecgraph callers "Модуль.Метод" --tenant-id demo
# verify-харнес ролевых возможностей: uv run python scripts/verify_demo.py <tenant> → scripts/verify_<tenant>.out.json
uv run onec-vecgraph snapshot "<путь>" --out snapshots/x.json               # слепок configVersion
uv run onec-vecgraph snapshot-diff before.json after.json                   # дифф изменений
uv run onec-vecgraph serve --transport http                                  # MCP по HTTP (:8000/mcp)
```

## 13. Гочи (на чём спотыкались)

- **torch на Windows зависает при выходе** после тяжёлой работы на GPU → в CLI `vectorize`/`search`
  стоит `_flush_exit()`=`os._exit(0)` после печати (данные уже закоммичены). Не убирать.
- **rich падал на cp1251** на символе `▸` → в `cli.py` `sys.stdout.reconfigure(utf-8)` + `Console(legacy_windows=False)`.
- **`docker compose up -d`** не пересоздаёт контейнер при смене env → нужен `--force-recreate`.
- **uv.toml** не допускает `[sources]` → весь uv-конфиг в `pyproject [tool.uv]`; `uv.toml` удалён.
- **Большое `DETACH DELETE`** превышает лимит памяти транзакции Neo4j → удаления батчами (delete_tenant/
  delete_chunks/delete_routines).
- **`expandable_segments` (PYTORCH_CUDA_ALLOC_CONF)** на Windows игнорируется; OOM лечится лимитом длины
  (`max_seq_length=256`) + батчем.
- Если осиротевший python держит VRAM (после зависшего фон-прогона) — убить через Task Manager (из
  неэлевированной сессии может быть Access denied).
- **`pytest` без аргументов собирает 0 тестов** (в pyproject нет `[tool.pytest.ini_options] testpaths`) →
  запускать `uv run pytest tests/`. Добавлять секцию в pyproject НЕЛЬЗЯ оффлайн: **любая правка pyproject
  заставляет `uv run` пересобирать пакет** (тянет hatchling из сети → таймаут). Если правил pyproject и сети
  нет — `uv run --no-sync …`.
- **Точный фильтрованный поиск** (`exact_vector_search`) использует `vector.similarity.cosine` (Neo4j ≥5.18) —
  на 5.26 ок; на старых версиях упадёт (тогда только индексный путь).
