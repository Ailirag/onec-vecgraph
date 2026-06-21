# Overlay tenants — baseline (release) + per-task developer delta

> Реализуемый контракт интеграции для оркестратора (эндпоинты, аргументы `index_overlay`, формат
> ответа, правила чтения) — [ORCHESTRATOR_CONTRACT.md](ORCHESTRATOR_CONTRACT.md). Ниже — модель и устройство.

Поддержка двухслойной модели тенантов для оркестратора «Full development pipeline»: тяжёлая
релизная поставка (**baseline**) индексируется один раз, а правки разработчика по задаче кладутся
как дешёвая **дельта** в эфемерный overlay-тенант — без полной ре-векторизации на каждого разработчика.

## Модель
- **Baseline** `<base>` (напр. `grand-dev-mdm@release`) — полный `index + callgraph + vectorize`, read-only.
  Индексируется офлайн-CLI **или** оркестратором через отдельный admin-эндпоинт `reindex_baseline`
  (fire-and-poll, порт 8002) — см. [ORCHESTRATOR_CONTRACT.md §10](ORCHESTRATOR_CONTRACT.md). Этот эндпоинт
  отделён от overlay-write: overlay пишет per-task дельту, admin (пере)индексирует базу целиком.
- **Overlay** `<base>@task/<task_id>` — только изменённые/добавленные объекты выгрузки разработчика
  (touched) + tombstones удалённых. Эфемерный, дропается по завершении задачи. Это **обычный tenant**
  в той же Neo4j (ключ `(tenant_id, fqn)`), модель/размерность эмбеддингов общая с baseline.

Разделение «фильтр vs изоляция» сохраняется: тенант — серверная граница; overlay при чтении задаётся
аргументом, но **валидируется** против неймспейса вызывающего (`<base>@task/*`), см. ниже.

## Запись: отдельный write-endpoint (read-сервер остаётся read-only)
`onec-vecgraph serve-write` поднимает **отдельный** FastMCP-сервер (свой порт `WRITE_MCP_PORT`),
который экспонирует единственный write-инструмент `index_overlay`. Включается только
`OVERLAY_WRITE_ENABLED=true`. Read-сервер (`serve`) не меняется и пишущих инструментов не имеет.

**Авторизация неймспейса.** `WRITE_AUTH_TOKENS="<token>=<base>"` — токен разрешает писать только в
overlay под этим base (`<base>@task/*`). `index_overlay` проверяет `in_namespace(tenant_id, base)` →
ни baseline, ни чужой проект записать нельзя. Без write-токенов (dev) запись разрешена, но всё равно
только в overlay-тенант (`@task/` обязателен).

### MCP tool `index_overlay`
Аргументы (см. dev-overlay-tenant-design оркестратора): `tenant_id` (overlay), `base_tenant_id`,
`roots[]` (корни dev-выгрузки), `files[{key,path,kind,name}]` (touched), `deleted[]` (object-keys),
`options{build_graph,vectorize}`. Поведение:
1. **Только touched** → объекты (маппинг файла в объект через наш `enumerate_objects`; для модулей/форм —
   к объекту-владельцу; для удалений — `fqn_from_object_key`). Per-object upsert (`scoped_delete`+rebuild),
   **без reset тенанта**.
2. Граф/`callgraph`/`vectorize` затронутых — той же моделью, что baseline (overlay-тенант мал → дёшево).
3. **Tombstones** (`:Tombstone (tenant_id, fqn)`) для `deleted`.
4. `structuredContent`-сводка: `indexed_files`+`indexed_objects`/`deleted`/`chunks`/`embedding_model`/`embedding_dim`/`unresolved`/`base_tenant_id` (`indexed_files` — ключ из контракта оркестратора; `indexed_objects`=число затронутых объектов — additive).
Ошибки (выключено, нет прав, не overlay-тенант, парсинг) → MCP `isError`.

CLI-зеркало для офлайн-теста: `onec-vecgraph index-overlay <payload.json>`.

## Чтение: слияние baseline ∪ overlay
- **Поиск (Phase 1)** — слияние делает оркестратор: дважды зовёт `hybrid_search` (baseline + overlay,
  каждый своим токеном) и мёржит. Наши хиты уже несут совместимые поля (`fqn`/`routine_fqn`, `kind`/`corpus`,
  `rrf_score`, `tenant`) — доработок не нужно.
- **Граф (Phase 2)** — union на нашей стороне. Графовые инструменты принимают `overlay_tenant_id`:
  `get_dependencies` / `impact_analysis` / `find_callers` / `find_callees` / `call_graph`.
  Правило union: ребро «живёт» в тенанте, владеющем текущей версией его **источника** (overlay, если
  источник touched; иначе baseline); tombstones исключают объект и рёбра в него; каждая строка несёт
  `layer` (`release`/`working`). Исходящие рёбра объекта следуют его текущей версии; входящие — мёржатся
  по владельцу-источнику.
- **Anti-leak**: `overlay_tenant_id` валидируется `in_namespace(overlay, caller_tenant)` — читать можно
  только overlay **под своим** base.

### Граница Phase 2 (известное ограничение v1)
`callgraph` overlay-тенанта резолвит вызовы в пределах touched-объектов (+ их общих модулей в overlay).
Рёбра «touched-рутина → неизменённая baseline-рутина» в v1 могут быть не разрешены (overlay строится без
baseline-контекста). Поэтому `find_callees` с overlay **доливает** baseline-callees для recall (overlay
выигрывает). `call_path` остаётся однотенантным (кросс-слойный путь не объединяется) — для overlay-анализа
вызовов используйте `call_graph`. Полное разрешение «cross-layer» — последующая доработка callgrapher
(передавать baseline routine-index в сборку overlay).

## Жизненный цикл (оркестратор → onec-vecgraph)
```
create-task     → overlay-тенант '<base>@task/<id>'
overlay-refresh → дельта (touched/deleted) у оркестратора
index-overlay   → write-endpoint: index_overlay(touched/deleted) в overlay-тенант (+tombstones)
run-role        → hybrid_search(base)+hybrid_search(overlay) merge; графы — *_with overlay_tenant_id
approve(last)   → задача закрыта → overlay дропается; дельта войдёт в следующий релизный baseline
```

## Реконсиляция контракта с деплой-скелетом оркестратора
- **Env-имена**: приняты алиасы — `NEO4J_USERNAME`→`neo4j_user`, `EMBEDDINGS_PROVIDER`→`embedding_provider`,
  `ONEC_VECGRAPH_TENANT_ID`→`default_tenant_id` (оба написания работают). `MCP_TRANSPORT` у нас — флаг CLI
  (`serve --transport`), не env.
- **Имена инструментов**: добавлен `call_graph`. Для чек-листа оркестратора: `metadata_search`≈`list_metadata`,
  `type_usage_search`≈`find_type_usages` (алиасы не заводили — используйте фактические имена).

## Конфиг (.env)
```
OVERLAY_WRITE_ENABLED=true                 # включить write-endpoint
WRITE_MCP_PORT=8001                        # порт write-сервера
WRITE_AUTH_TOKENS=wtok=grand-dev-mdm@release   # token=base namespace (overlay только '<base>@task/*')
```
Запуск (CLI): `onec-vecgraph serve` (read) и отдельно `onec-vecgraph serve-write` (write).
Запуск (Docker): read-сервис `app` (порт 8000) поднимается всегда; overlay-write — сервис `app-write`
(порт 8001) под compose-профилем `overlay-write`: `docker compose --profile overlay-write up -d`
(`OVERLAY_WRITE_ENABLED=true` сервис проставляет сам). Детали — [docs/DEPLOYMENT.md §4.1](DEPLOYMENT.md).

## Код
- `overlay.py` — неймспейс тенантов, `in_namespace` (write/anti-leak guard), `map_paths_to_object_fqns`, `fqn_from_object_key`.
- `overlay_index.py` — драйвер `index_overlay` (touched→объекты→граф/callgraph/vectorize→tombstones→summary).
- `write_server.py` — отдельный FastMCP write-сервер (tool `index_overlay`).
- `queries.py` — `_overlay_sets`/`_merge_edge_rows`/`_tag_layer`, union в `get_dependencies`/callers/callees/`call_graph`.
- `storage/neo4j_store.py` — `write_tombstones`/`clear_tombstones`/`tombstoned_fqns`; `graph/schema.py` — метка `:Tombstone`.
- `cli.py` — `serve-write`, `index-overlay`.
