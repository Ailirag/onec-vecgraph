# Задание: baseline-реиндексация через MCP (`reindex_baseline` + `index_job_status`)

> Самодостаточный бриф для сессии доработки `onec-vecgraph` (перенесён из оркестратора
> «Full development pipeline»). **Статус: реализовано.** Реализацию и контракт см.
> [docs/ORCHESTRATOR_CONTRACT.md §10](../ORCHESTRATOR_CONTRACT.md), модули
> `src/onec_vecgraph/{admin_server,baseline,jobs}.py`, тесты `tests/test_baseline.py`.

## Контекст
Оркестратор «Full development pipeline» добавляет запуск векторизации/перестроения графа из UI с
расписанием. **Overlay-путь уже полностью поддержан** в onec_vecgraph (`write_server.py` → тул
`index_overlay`) — доработка не нужна, только развёртывание write-сервиса `:8001` + `WRITE_AUTH_TOKENS`.
**Эта доработка нужна для baseline-сценария:** полный baseline (`index`/`callgraph`/`vectorize`) раньше
был только offline-CLI (`cli.py`), MCP-тула не было, поэтому оркестратор не мог запускать/мониторить
полную переиндексацию из UI.

## Цель
Дать оркестратору запускать и **мониторить** полную (ре)индексацию baseline-тенанта — `index` →
`callgraph` → `vectorize` — **через MCP**, не заходя в контейнер (`docker exec`) и не держа соединение
часами. Запуск асинхронный (часы на ERP-масштабе), статус — поллингом.

## Объём работ (deliverables)
1. **MCP-тул запуска baseline** на отдельном maintenance/admin-эндпоинте (НЕ на read-сервере).
   `reindex_baseline(tenant_id, source|roots, options:{steps, reset:false, batch_size, embedding_model?}) ->
   {job_id, accepted:true}`. Возвращает СРАЗУ `job_id` (fire-and-poll), работа уходит в фон. Opt-in
   env-флаг (`BASELINE_REINDEX_ENABLED=true`) + auth (`ADMIN_TOKENS`, скоуп на base). `reset:true` — только
   по явному флагу (`confirm_reset`).
2. **Тул статуса джобы:** `index_job_status(job_id) -> {status, phase, counts{objects,nodes,edges,routines,
   chunks}, percent, started_at, finished_at, error, embedding_model, embedding_dim, files_missing}`. Persist
   состояния (переживает отдельные вызовы).
3. **Сериализация и pool-safety:** server-side single-flight/очередь на baseline-джобы (общий GPU).
   `index_job_status` отражает очередь.
4. **Контракт сводки:** поля результата согласованы с overlay (`indexed_objects`, `chunks`, `graph_updated`,
   узлы/рёбра/рутины, `embedding_model/dim`, `unresolved`/`files_missing`). Явно сигналить «пустой граф /
   files_missing» → оркестратор пометит прогон `warning` (главный пилотный баг — рассинхрон mount-путей).
5. **Health/whoami** на maintenance-эндпоинте для readiness-probe.
6. **Документация:** `docs/ORCHESTRATOR_CONTRACT.md`, `docs/DEPLOYMENT.md`, `docs/OVERLAY.md`/`docs/STATE.md`.
7. **Тесты:** unit/integration новых тулов и фоновой джобы (auth-скоупинг, отказ при `reset` без флага,
   сериализация двух джоб, статусные переходы).

## Контракт с оркестратором
`baseline_index_tool: "reindex_baseline"`, `baseline_status_tool: "index_job_status"`, `baseline_index_url`
(порт 8002). Оркестратор: `reindex_baseline` → сохранить `job_id` → поллить `index_job_status` до
терминального статуса, показать фазы/сводку.

## Вне объёма
- Read-сервер остаётся read-only (не трогать).
- Overlay-тул `index_overlay` — уже готов, не переписывать.
- Оркестраторная обвязка (очередь прогонов, расписание, UI) — отдельная сессия в full-development-pipeline.

## Критерии приёмки
- Из MCP можно запустить `reindex_baseline` для тенанта и довести до конца через поллинг
  `index_job_status` с фазами и финальной сводкой.
- Конкурентные baseline-джобы сериализуются; auth-скоуп по base enforced; `reset` требует явного флага.
- `files_missing`/пустой граф различимы в ответе.
- `docs/ORCHESTRATOR_CONTRACT.md` обновлён; тесты зелёные.

## Как реализовано (итог)
- **Отдельный admin-сервер**: `src/onec_vecgraph/admin_server.py` (FastMCP), CLI `serve-admin`, порт 8002,
  профиль compose `baseline-admin`, opt-in `BASELINE_REINDEX_ENABLED`. Read-сервер (8000) и overlay-write
  (8001) не затронуты.
- **Auth**: отдельная карта `ADMIN_TOKENS="tok=<base>"` (`tenancy.resolve_admin_base`); запись baseline
  разрешена только владельцу base. Семантически ≠ `WRITE_AUTH_TOKENS` (тот — только overlay).
- **Reset-guard**: `options.reset:true` honor-ится только с `options.confirm_reset:true`.
- **Драйвер** `baseline.py`: `validate_reindex_request` (чистый гард), `run_baseline_reindex` (обёртка над
  `indexer.index_dump`/`callgrapher.build_call_graph`/`vectorizer.vectorize` напрямую, с `on_progress`),
  `final_status` (`files_missing|empty_graph → warning`).
- **Джобы** `jobs.py`: `JobStore` (in-memory + опц. JSON-persist `BASELINE_JOBS_PATH`, running→failed при
  рестарте) + `BaselineRunner` (single-flight FIFO worker → глобальная сериализация на общем GPU; дубликат
  того же тенанта отклоняется с `active_job_id`).
- **Тесты**: `tests/test_baseline.py` (чистые юнит-тесты без Neo4j).
