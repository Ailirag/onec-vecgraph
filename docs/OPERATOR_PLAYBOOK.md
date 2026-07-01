# onec-vecgraph — операторский плейбук (управление векторизацией)

**Единый источник правды** для управления *записью* в базу знаний 1С (Neo4j: граф + векторы + полнотекст).
Точки входа разных агентов (Claude skill, Codex `AGENTS.md`, Copilot prompt-файл) ссылаются сюда — правьте процедуры здесь.

Сам MCP-сервер **read-only**; индексацию/векторизацию запускает **только этот CLI** (`uv run onec-vecgraph …`).

## Золотые правила (нарушение → тихая порча данных)
1. **Один инвариант модели/размерности на БД.** `EMBEDDING_PROVIDER`+`EMBEDDING_MODEL` при `vectorize`/`ingest`
   ДОЛЖНЫ совпадать с тем, что в `.env` у работающего сервера. Векторный индекс один на всю БД; смешение
   моделей/размерностей ломает поиск. Сменить модель на существующей БД = **полный реиндекс всех тенантов**.
2. **Tenant = (организация × конфигурация).** Разные конфигурации → разные `--tenant-id` (напр. `acme_erp`, `acme_ut`).
   `config_id` (`base` | `ext:<имя>`) — это только база/расширение **одной** конфигурации, НЕ граница изоляции.
   Две разные конфигурации в одном тенанте схлопнутся по `fqn` — не делать.
3. **Публичная справка платформы/БСП → общий тенант `__shared__`** (читается всеми аддитивно). Не дублировать на проект.
   `__shared__` эмбеддить ТОЙ ЖЕ моделью/размерностью, что и проекты-потребители.
4. **Секреты.** `.env` в `.gitignore` — не коммитить, токены/пароли не светить. Коммит/пуш — **только по явной просьбе пользователя**.

## Предусловия
- **Окружение сессии:** перед любыми командами — [SESSION_BOOTSTRAP.md](SESSION_BOOTSTRAP.md) (uv в PATH,
  UTF-8, кеш моделей, `.venv` в git-worktree) + таблица «симптом → фикс». Префикс в каждую команду:
  `$env:Path="D:\tools\uv;$env:Path"; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:PYTHONUTF8='1'; $env:HF_HOME='D:\tools\hf-cache'`.
  Быстрый предполёт (интерактивно): `. .\scripts\preflight.ps1 -StartNeo4j`.
- Neo4j поднят: `docker compose up -d neo4j`; проверка — `uv run onec-vecgraph health`.
- `.env` настроен (`EMBEDDING_PROVIDER/MODEL/DEVICE`, `NEO4J_*`). Для GPU: `EMBEDDING_DEVICE=cuda` + torch cu128 + `--gpus`.
- **Инвокация:** `uv run onec-vecgraph <cmd>`.
  - Гоча: если редактировался `pyproject.toml`/зависимости и `uv` пытается офлайн-пересборку и падает —
    `uv run --no-sync onec-vecgraph <cmd>` (зависимости уже установлены).
- Windows/PowerShell: пути с пробелами — в кавычках. Тяжёлые GPU-команды (`vectorize`) сами делают `os._exit(0)`
  после печати результата — это норма, не зависание.

## 1. Загрузить НОВУЮ конфигурацию (полный конвейер)
Порядок строгий: **index → callgraph → vectorize**.
```
uv run onec-vecgraph index "<путь к выгрузке XML>" --tenant-id acme_erp --reset
uv run onec-vecgraph callgraph --tenant-id acme_erp
uv run onec-vecgraph vectorize --tenant-id acme_erp --code
```
- `index --reset` — очистить граф тенанта перед загрузкой (чистая первичная загрузка). Без `--reset` — догрузка.
- `vectorize` по умолчанию ПЕРЕСТРАИВАЕТ чанки; `--code` добавляет векторизацию BSL по рутинам (модули объектов/общие/формы).
- `index --config-release "ERP_2.5.18"` — проставить релиз конфигурации как `corpus_version=config:<релиз>` на объектах (owner-фасет для фильтра поиска `corpus_version`; без флага не ставится).

## 2. Инкрементальное обновление после правок (configVersion-based, безопасно)
```
uv run onec-vecgraph index "<путь>" --tenant-id acme_erp --incremental
uv run onec-vecgraph callgraph --tenant-id acme_erp --incremental
uv run onec-vecgraph vectorize --tenant-id acme_erp --incremental --code
```
- `vectorize --incremental` ИГНОРИРУЕТ reset и переэмбеддит только объекты с изменившимся `configVersion` — неизменённое не трогает.
- `callgraph --incremental` переразбирает только изменённые; если изменился **общий модуль** — откатывается к полному разбору (by design).
- Верификация дельты (configVersion-снимки):
```
uv run onec-vecgraph snapshot "<путь>" --out snapshots/before.json   # до правок
uv run onec-vecgraph snapshot "<путь>" --out snapshots/after.json    # после новой выгрузки
uv run onec-vecgraph snapshot-diff snapshots/before.json snapshots/after.json
```

## 3. Долить векторизацию кода поверх готовых метаданных (без перезатирания)
```
uv run onec-vecgraph vectorize --tenant-id acme_erp --no-reset --code
```
`--no-reset` сохраняет существующие чанки и добавляет code-чанки (не пере-эмбеддит метаданные заново).

## 4. Doc-корпуса: ИТС / проектные артефакты (по манифесту)
```
uv run onec-vecgraph ingest <manifest.yaml> --tenant-id acme_erp                  # инкремент по version_hash
uv run onec-vecgraph ingest <manifest.yaml> --tenant-id acme_erp --only its       # только один тип источника
uv run onec-vecgraph ingest <manifest.yaml> --tenant-id acme_erp --reset          # пересобрать корпуса
uv run onec-vecgraph ingest <manifest.yaml> --tenant-id acme_erp --link-semantic  # + RELATES_TO к ближайшим объектам
```
`--only` ∈ `config_dump | its | git_artifacts`. Контракт парсера ИТС — [ITS_PARSER_REQUIREMENTS.md](ITS_PARSER_REQUIREMENTS.md).

> **Целевые рецепты по корпусам знаний** (ИТС по конфигурации / стандарты разработки / справка платформы по версии),
> с пошаговым agent-runnable планом, верификацией и разбором ошибки «владельцы без чанков» —
> [VECTORIZATION_GUIDE.md](VECTORIZATION_GUIDE.md).

### 4b. Стандарты разработки 1С (v8std) → `__shared__`
Непроектный корпус «как писать по стандартам 1С». Грузится в общий тенант, читается всеми; манифест —
[`manifests/its-v8std.yaml`](../manifests/its-v8std.yaml). Инструменты чтения: `dev_standards_search` / `dev_standards_get`.
```
uv run onec-vecgraph ingest manifests/its-v8std.yaml --tenant-id __shared__ --reset
```
- `--reset` здесь удаляет ТОЛЬКО `source='its'` (`delete_source('its')`) — справка платформы не затрагивается.
- Гоча: владельцы-стандарты с тем же `version_hash` инкрементом ПРОПУСКАЮТСЯ → если чанков нет, нужен `--reset`.
- Проверка: `dev_standards_search("…")` непуст; `dev_standards_get("396")` отдаёт непустой `text`. Детали — [VECTORIZATION_GUIDE.md §B](VECTORIZATION_GUIDE.md).

**Классификация (owner-фасеты для фильтрованного поиска).** В записи источника манифеста можно задать `doc_topic`
(`platform`/`config`/`task`) и `corpus_version` (напр. `config:ERP_2.5.18`, `task:JIRA-1234`); для ИТС их также может
проставлять парсер по-записям (см. контракт). Дефолты: ИТС → `doc_topic=config`, артефакты git → `doc_topic=task`.
Размещение по-прежнему решает тенант: доки, которые должны линковаться с объектами проекта, грузить в тенант проекта,
а не в `__shared__` (линковка `MENTIONS`/`RELATES_TO` внутритенантна).

## 5. Справка платформы (.hbk) → общий тенант `__shared__` (версионно; путь валидируется до старта)
```
uv run onec-vecgraph ingest-help --tenant-id __shared__ --bin "C:\Program Files\1cv8\8.3.27.1989\bin" --domain shcntx --domain shlang
uv run onec-vecgraph ingest-help --tenant-id __shared__ --file "<...>\shcntx_ru.hbk" --platform-version 8.3.27.1989   # явные файлы
```
- `--domain`: `shcntx` (контекст/объекты), `shlang` (язык), `shquery` (запросы). Дефолт — `shcntx`+`shlang`.
- `--limit N` — смоук-загрузка; `--reset` — пересобрать эту версию справки.
- Если путь не указан / `.hbk` не найден — команда падает с явной ошибкой (не делает «тихо ничего»).
- Проверка: `uv run onec-vecgraph platform-docinfo "Массив.Найти" --tenant-id acme_erp` (читает `__shared__` аддитивно).

## 5a. Overlay (baseline + per-task дельта разработчика; см. docs/OVERLAY.md)
```
# Пишущий эндпоинт (отдельный сервер; read-сервер остаётся read-only):
OVERLAY_WRITE_ENABLED=true uv run onec-vecgraph serve-write --transport http   # порт WRITE_MCP_PORT (8001)
# В Docker: сервис app-write под профилем (OVERLAY_WRITE_ENABLED ставит сам, порт 8001):
docker compose --profile overlay-write up -d
# Инкрементальная индексация дельты в overlay-тенант '<base>@task/<id>' (оффлайн-зеркало MCP-инструмента):
uv run onec-vecgraph index-overlay payload.json
# Релиз baseline индексируется обычным index/callgraph/vectorize; overlay наполняет ТОЛЬКО touched-объекты.
```
- Запись разрешена только в overlay-тенант (`@task/`); `WRITE_AUTH_TOKENS="token=base"` ограничивает namespace.
- Чтение со слиянием: графовые инструменты принимают `overlay_tenant_id` (Phase 2); поиск сливает оркестратор.

## 5b. Онбординг тенанта (провижининг токенов, admin :8002)
Атомарная связка «оркестратор ↔ векторная БД»: завести тенанта и выдать bearer-токен одним вызовом (без ручного
вписывания токенов в env, без плейсхолдер-`tenant_id`). Требует `PROVISIONING_ENABLED=true` + `AUTH_ENABLED=true`
(иначе токен на :8000 не применится) + admin-токен (`ADMIN_TOKENS`) как контрол-плейн. Инструменты admin-сервера:
- **`provision_tenant({tenant_id, config_id?, display_name?, rotate?})`** → `{tenant_id, config_id, token, created}`.
  Идемпотентно: `created=false` если уже был; `rotate:true` — перевыпуск (старый инвалидируется). **Секрет-токен
  возвращается ОДИН раз** (в Neo4j хранится только sha256): при `created=true`/`rotate=true` — новый токен, иначе `token:null`.
- **`list_tenants()`** → `{tenants:[{tenant_id, config_ids, has_token}]}` — аудит.
- **`revoke_tenant_token({tenant_id})`** → деактивация (вступает в силу на read/write в пределах `TOKEN_CACHE_TTL_SECONDS`).

Выданный токен пригоден как `Authorization: Bearer` на :8000 (read своего тенанта + аддитивно `__shared__`),
:8001 (overlay-write своего base) и :8002 (self-admin: reindex своего baseline / whoami). **Провижинить чужие
тенанты проектный токен не может** — это только контрол-плейн (env `ADMIN_TOKENS`). Персистентные узлы
`:Tenant`/`:TenantToken` НЕ трогаются `delete_tenant` (baseline-reset не отзовёт токен).
> **Включение `AUTH_ENABLED=true` на живом инстансе** делает токены обязательными: сначала провижинь токены
> действующим тенантам (`erp/erp_test/ut/demo`), иначе клиенты без токена «тихо увидят пустоту».

## 6. Поднять сервер / проверка / диагностика
```
uv run onec-vecgraph serve --transport http          # http://127.0.0.1:8000/mcp (read-only)
uv run onec-vecgraph serve --transport stdio         # локальные MCP-клиенты
uv run onec-vecgraph health                          # связность Neo4j
uv run onec-vecgraph metrics --tenant-id acme_erp    # объекты/код/рёбра графа/хотспоты
uv run onec-vecgraph search "..." --tenant-id acme_erp   # дымовой поиск
uv run onec-vecgraph ls --tenant-id acme_erp --kind Catalog
```
**Пустой результат поиска/графа вызовов ⇒ обычно «слой не построен для этого тенанта», а не «не найдено».**
Сначала `metrics`: есть ли chunks/routines.

## Семантика reset/incremental (асимметрична — запомнить)
| Команда | reset по умолчанию | флаги |
|---|---|---|
| `index` | ВЫКЛ | `--reset` очистить тенант; `--incremental` только изменённые |
| `callgraph` | ВКЛ (перестраивает) | `--no-reset` сохранить; `--incremental` только stale (общий модуль → полный) |
| `vectorize` | ВКЛ (перестраивает) | `--no-reset` сохранить/долить; `--incremental` только stale (reset игнорируется) |

## Чего НЕ делать
- Не менять `EMBEDDING_MODEL`/`PROVIDER` на существующей БД без полного реиндекса всех тенантов (несовпадение размерности ломает единый векторный индекс).
- Не пытаться векторизировать через MCP — этого инструмента там нет by design; только CLI.
- Не класть две разные конфигурации в один tenant (коллизия `fqn`).
- Не коммитить `.env`/токены; не пушить без явной просьбы.

## Глубже
- **Рецепты векторизации корпусов знаний (agent-runnable)** — [VECTORIZATION_GUIDE.md](VECTORIZATION_GUIDE.md).
- Настройки и деплой — [DEPLOY_DETAILED.md](DEPLOY_DETAILED.md), [DEPLOYMENT.md](DEPLOYMENT.md), `.env.example`.
- Как РОЛИ-потребители читают данные (read-only) — [MCP_USAGE.md](MCP_USAGE.md).
- Снимок состояния и инварианты — [STATE.md](STATE.md).
