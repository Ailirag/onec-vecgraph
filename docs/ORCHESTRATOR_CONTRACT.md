# Контракт `onec-vecgraph` ↔ оркестратор «Full development pipeline»

Единый реализуемый контракт интеграции: эндпоинты, аутентификация, модель тенантов, пишущий
инструмент `index_overlay`, чтение со слиянием `baseline ∪ overlay`, инварианты. Концептуальная модель
overlay — [OVERLAY.md](OVERLAY.md); развёртывание write-сервера в Docker — [DEPLOYMENT.md §4.1](DEPLOYMENT.md);
потребительский гайд по инструментам — [MCP_USAGE.md](MCP_USAGE.md).

Всё ниже сверено с кодом: `write_server.py`, `overlay_index.py`, `overlay.py`, `tenancy.py`, `server.py`.

---

## 1. Эндпоинты и транспорт

| Назначение | Сервис (CLI / compose) | Адрес | Режим |
|---|---|---|---|
| **Чтение** (поиск, граф) | `serve` / `app` | `http://host:8000/mcp` | MCP Streamable HTTP, stateless, **read-only** |
| **Запись overlay** | `serve-write` / `app-write` | `http://host:8001/mcp` | MCP Streamable HTTP, stateless, единственный tool `index_overlay` |

Write-сервис **opt-in**: в Docker — compose-профиль `overlay-write`
(`docker compose --profile overlay-write up -d`), либо CLI
`OVERLAY_WRITE_ENABLED=true onec-vecgraph serve-write --transport http`. Оба порта слушают loopback —
наружу только через TLS-прокси.

## 2. Аутентификация

**Чтение** (`app`):
- `AUTH_ENABLED=true` → каждый вызов несёт `Authorization: Bearer <token>`; tenant берётся из карты
  `AUTH_TOKENS="tok=tenant[:config]"` на сервере (заголовок `X-Tenant-Id` игнорируется — подделать нельзя).
- `AUTH_ENABLED=false` → доверенный `X-Tenant-Id` (только за аутентифицирующим gateway).

**Запись** (`app-write`):
- `WRITE_AUTH_TOKENS="wtok=<base>"` → токен авторизует запись **только** в overlay под этим base
  (`<base>@task/*`). Ни baseline, ни чужой проект записать нельзя.
- Без write-токенов — dev-режим: запись разрешена, но всё равно только в overlay-тенант (`@task/` обязателен).
- Заголовок: `Authorization: Bearer <wtok>`.

## 3. Модель тенантов

| Слой | Ключ | Кто индексирует | Свойства |
|---|---|---|---|
| **Baseline** | `<base>` (напр. `grand-dev-mdm@release`) | оператор офлайн (`index`+`callgraph`+`vectorize`) | read-only, полная поставка |
| **Overlay** | `<base>@task/<task_id>` (разделитель `@task/`) | оркестратор через `index_overlay` | эфемерный, только touched-объекты + tombstones |

Правило неймспейса (`in_namespace`): токен/тенант base `<base>` → доступ только к `<base>@task/<любое>`.
Хелперы: `overlay_tenant_id(base, task_id)` строит ключ; `base_tenant_of` / `task_of` разбирают его.

## 4. Жизненный цикл

```
create-task     → оркестратор формирует overlay-тенант '<base>@task/<id>'
overlay-refresh → оркестратор вычисляет дельту (touched/deleted) из dev-выгрузки
index-overlay   → write-эндпоинт: index_overlay(...) → overlay-тенант (+tombstones)
run-role        → чтение: hybrid_search(base) + hybrid_search(overlay) merge;
                  графы — инструменты с overlay_tenant_id (union baseline ∪ overlay)
approve(last)   → задача закрыта → overlay дропается; дельта войдёт в следующий baseline
```

## 5. Запись: MCP-инструмент `index_overlay`

**Аргументы:**

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `tenant_id` | string | да | overlay-тенант `<base>@task/<id>` (должен содержать `@task/`) |
| `roots` | string[] | да | корни dev-выгрузки (абсолютные пути внутри контейнера) для маппинга путь→объект |
| `files` | object[] | да* | touched-объекты: `[{key, path, kind?, name?}]` (формат key — §6) |
| `deleted` | string[] | нет | object-keys удалённых объектов → tombstones |
| `base_tenant_id` | string | нет | baseline, к которому относится overlay (возвращается эхом) |
| `options` | object | нет | `{build_graph: bool=true, vectorize: bool=true}` |
| `project_id`, `task_id`, `base_source`, `dev_source` | string | нет | принимаются для совместимости контракта; в v1 драйвером не используются |

\* `files` может быть пустым, если в дельте только удаления.

**Поведение:**
1. Резолв `files` → fqn объектов: по `roots` строится индекс `enumerate_objects` (точное совпадение XML
   или префикс каталога объекта; модули/формы → объект-владелец); fallback — `fqn_from_object_key(key)`.
   Per-object upsert (`scoped_delete` + rebuild), **без reset тенанта**.
2. `deleted` → объект удаляется из overlay целиком + пишется `:Tombstone (tenant_id, fqn)`
   (маскирует baseline-объект при Phase-2 чтении). «Воскресший» объект теряет свой tombstone.
3. `build_graph=true` → `callgraph` overlay (он мал → дёшево); `vectorize=true` → векторизация **той же
   моделью/размерностью**, что baseline.
4. Возврат структурированной сводки (`structuredContent`).

**Пример запроса (аргументы tool):**
```json
{
  "tenant_id": "grand-dev-mdm@release@task/T-1024",
  "base_tenant_id": "grand-dev-mdm@release",
  "roots": ["/dev/T-1024/src"],
  "files": [
    {"key": "0/Catalogs/Контрагенты.xml", "path": "/dev/T-1024/src/Catalogs/Контрагенты.xml", "kind": "Catalog", "name": "Контрагенты"},
    {"key": "0/CommonModules/РаботаСКонтрагентами/Ext/Module.bsl", "path": "/dev/T-1024/src/CommonModules/РаботаСКонтрагентами/Ext/Module.bsl"}
  ],
  "deleted": ["0/Reports/УстаревшийОтчёт.xml"],
  "options": {"build_graph": true, "vectorize": true}
}
```

**Ответ (`structuredContent`):**
```json
{
  "tenant_id": "grand-dev-mdm@release@task/T-1024",
  "base_tenant_id": "grand-dev-mdm@release",
  "indexed_files": 2,
  "indexed_objects": 2,
  "deleted": 1,
  "chunks": 37,
  "graph_updated": true,
  "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
  "embedding_dim": 1024,
  "unresolved": []
}
```

| Поле ответа | Смысл |
|---|---|
| `indexed_files` | число входных файлов (ключ контракта оркестратора) |
| `indexed_objects` | число фактически затронутых объектов (additive) |
| `deleted` | число записанных tombstones |
| `chunks` | сколько чанков перевекторизовано |
| `graph_updated` | пересобирался ли callgraph |
| `embedding_model` / `embedding_dim` | модель и размерность (контроль совпадения с baseline) |
| `unresolved` | keys/paths, не сопоставленные объекту — требуют внимания оркестратора |

**Ошибки → MCP `isError`:** write выключен (`OVERLAY_WRITE_ENABLED=false`); токен не авторизует
namespace `tenant_id`; `tenant_id` не overlay (нет `@task/`); ошибки парсинга/размерности.

**CLI-зеркало (офлайн-тест):** `onec-vecgraph index-overlay <payload.json>`.

## 6. Формат object-key (генерирует оркестратор)

`<root_index>/<TypeFolder>/<Name>.xml`, где `root_index`: `0` = основная конфигурация, далее
расширения по индексу.
- Объект: `0/Catalogs/Контрагенты.xml` → `Catalog.Контрагенты`
- Модуль/форма → объект-владелец: `0/CommonModules/X/Ext/Module.bsl` → `CommonModule.X`
- Вложенные подсистемы: `0/Subsystems/A/Subsystems/B.xml` → `Subsystem.A.Subsystem.B`

`deleted` использует **только** `key` (файла уже нет → fqn выводится из ключа). Для `files` `path`
приоритетнее (точное совпадение XML или префикс каталога объекта), иначе `key`.

## 7. Чтение: слияние `baseline ∪ overlay`

**Phase 1 — поиск.** `hybrid_search` аргумент overlay **не принимает**; слияние делает **оркестратор**:
два вызова (baseline-токеном и overlay-токеном) + merge на своей стороне. Хиты совместимы по полям
(`fqn`/`routine_fqn`, `kind`/`corpus`, `rrf_score`, `tenant`); overlay побеждает по объекту.

**Phase 2 — граф (union на стороне сервиса).** Аргумент `overlay_tenant_id` принимают:
`get_dependencies`, `impact_analysis`, `find_callers`, `find_callees`, `call_graph`.
- Ребро «живёт» в тенанте-владельце текущей версии источника (overlay, если источник touched; иначе
  baseline); tombstones исключают объект и рёбра в него.
- Каждая строка несёт `layer` (`release` / `working`).
- **Anti-leak:** `overlay_tenant_id` валидируется `in_namespace(overlay, caller_tenant)` — читать можно
  только overlay **под своим** base, иначе ошибка.

**Известное ограничение v1 (Phase 2):** `callgraph` overlay резолвит вызовы в пределах touched-объектов;
рёбра «touched-рутина → неизменённая baseline-рутина» могут быть не разрешены. Поэтому `find_callees`
с overlay **доливает** baseline-callees (recall, overlay выигрывает). `call_path` — однотенантный;
для кросс-слойного анализа вызовов используйте `call_graph`.

**Стандарты разработки 1С (v8std).** Роль-агенты получают рекомендации «как писать по стандартам 1С»
через выделенные инструменты `search_standards(query)` → `get_standard(<номер>)` (обёртки над
`hybrid_search`/`get_document`, привязанные к корпусу `corpus_version="platform:v8std"` в общем тенанте).
Стандарты непроектные — читаются для **любого** тенанта автоматически, отдельного токена/overlay не требуют.
Загрузка корпуса — `ingest type: its` в `__shared__` (см. [DEPLOYMENT.md §5.3](DEPLOYMENT.md)).

## 8. Инварианты

- **Размерность эмбеддингов** baseline и overlay **обязана совпадать** (один векторный индекс на БД):
  overlay векторизуется той же моделью; `app-write` наследует `EMBEDDING_*` от общего блока compose.
- Overlay дропается по `approve` задачи (тенант удаляет оркестратор); tombstones живут только пока жив overlay.
- Запись — исключительно в `<base>@task/*`; baseline и чужие проекты недоступны на запись by design.

## 9. Реконсиляция имён (алиасы с деплой-скелетом оркестратора)

- Env-алиасы (оба написания работают): `NEO4J_USERNAME`→`NEO4J_USER`,
  `EMBEDDINGS_PROVIDER`→`EMBEDDING_PROVIDER`, `ONEC_VECGRAPH_TENANT_ID`→`DEFAULT_TENANT_ID`.
  `MCP_TRANSPORT` у нас — флаг CLI (`serve --transport`), не env.
- Имена инструментов: фактические — `list_metadata` (≈`metadata_search`), `find_type_usages`
  (≈`type_usage_search`), `call_graph`. Алиасы не заводились — используйте фактические имена.
