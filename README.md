# onec-vecgraph

MCP-сервер для **векторизации конфигураций 1С:Предприятие** (XML-выгрузка Конфигуратора **и**
проекты **1C:EDT**, база + расширения) и построения **графа зависимостей** (включая граф вызовов
BSL) в **Neo4j** — граф, векторы и полнотекст в одном хранилище. Мультиарендный, read-only MCP
по Streamable HTTP; запись (индексация/векторизация) — только CLI.

## onec-lite: MCP по живой рабочей копии — без Neo4j и векторов

Для машины разработчика есть **lite-режим**: 29 инструментов по живой рабочей копии
(Конфигуратор XML **и** EDT, база + расширения), код-анализ BSL-парсером, поиск ripgrep +
опц. FTS5 (BM25), справка платформы из `.hbk`, git-осведомлённость. Запуск — двойной клик
[`lite-admin.cmd`](lite-admin.cmd) или `uv run onec-lite admin` (пути задаются в браузере и
сохраняются); подключение к Claude Code: `claude mcp add onec-lite -- uv run --directory
"<путь к репо>" onec-lite`. **Руководство: [docs/LITE_USAGE.md](docs/LITE_USAGE.md).**

## Документация

| Документ | О чём |
|---|---|
| [docs/SERVICE_OVERVIEW.md](docs/SERVICE_OVERVIEW.md) | Точка входа: что это, архитектура за 30 секунд, карта документации |
| [docs/STATE.md](docs/STATE.md) | Живой снимок состояния: что готово, данные, ограничения, гочи |
| [docs/MCP_USAGE.md](docs/MCP_USAGE.md) | Гайд потребителя MCP (подключение, заголовки/auth, fqn, 29 инструментов, сценарии) |
| [docs/LITE_USAGE.md](docs/LITE_USAGE.md) | Руководство onec-lite (запуск, админка, инструменты, lite vs big) |
| [docs/OPERATOR_PLAYBOOK.md](docs/OPERATOR_PLAYBOOK.md) | Операторская сторона: index / callgraph / vectorize / ingest |
| [docs/VECTORIZATION_GUIDE.md](docs/VECTORIZATION_GUIDE.md) | Рецепты корпусов: ИТС, стандарты v8std, справка платформы |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) · [docs/DEPLOY_RUNBOOK.md](docs/DEPLOY_RUNBOOK.md) · [docs/DEPLOY_DETAILED.md](docs/DEPLOY_DETAILED.md) | Docker, варианты образа (CPU/GPU/cloud), auth, пошаговый runbook |
| [docs/OVERLAY.md](docs/OVERLAY.md) · [docs/ORCHESTRATOR_CONTRACT.md](docs/ORCHESTRATOR_CONTRACT.md) | Overlay-тенанты per-task и контракт оркестратора (write/admin-серверы) |
| [PLAN.md](PLAN.md) · [AGENTS.md](AGENTS.md) | Полный план/архитектура · точка входа для AGENTS.md-совместимых агентов |

## Что умеет (кратко)

- **Граф метаданных** — объекты, реквизиты и типы, ТЧ, перечисления, предопределённые,
  подсистемы (квалиф. fqn), права ролей, подписки, владельцы, формы, `WRITES_TO` (движения);
  слой `:Detail` с полным набором `<Properties>` по запросу. Оба формата: Конфигуратор XML и EDT.
- **Граф вызовов BSL** — портируемый Python-парсер: `CALLS` (local / общие модули / менеджерные,
  с confidence), модули форм + `HANDLES`, точки входа (проведение/запись/…), `OVERRIDES` для
  переопределений расширений (`&Вместо/&Перед/&После/&ИзменениеИКонтроль`).
- **Векторизация и поиск** — мульти-вектор (имя × смысл) + полнотекст c токенизацией
  идентификаторов, слияние RRF, фильтры (вид/чанк/подсистема/источник/версия), GraphRAG-достройка
  контекста; чанки метаданных, форм, кода (cAST), подсистем и ролей. Эмбеддинги: локальные
  Qwen3 (CUDA) или облачные (OpenAI-совместимые / Voyage).
- **Документационные корпуса** — ИТС, git-артефакты проекта, справка платформы из `.hbk`
  (версионная), стандарты разработки v8std; общий тенант `__shared__` читается всеми аддитивно.
- **Мультиарендность и auth** — tenant из заголовка или bearer-токена (`AUTH_ENABLED`),
  runtime-провижининг тенантов с токенами в Neo4j; строгая изоляция арендаторов.
- **Инкрементальность** — по `configVersion` (Конфигуратор) / content-hash (EDT): обновляются
  только изменённые объекты, входящие связи сохраняются.
- **Контур оркестрации** — overlay-write сервер (per-task дельты поверх baseline, :8001) и
  admin-сервер (:8002): baseline-реиндекс по MCP (fire-and-poll), веб-дашборд джоб, провижининг.
- **29 read-only MCP-инструментов** — поиск, структура, зависимости/импакт, код и граф вызовов,
  документация по источникам, стандарты, метрики. Полный список — [docs/MCP_USAGE.md](docs/MCP_USAGE.md).
- **onec-lite** — всё то же по живой рабочей копии без инфраструктуры (см. выше).

Проверено на реальных базах: УТ (≈14,9k объектов, EDT с расширениями) и ERP/УХ (≈24k объектов).
Актуальный статус и ограничения — [docs/STATE.md](docs/STATE.md).

### Команды

```powershell
uv run onec-vecgraph index "<путь к выгрузке>" --tenant-id <t> --reset        # построить граф (Конфигуратор|EDT)
uv run onec-vecgraph index "<путь к выгрузке>" --tenant-id <t> --incremental  # только изменённые
uv run onec-vecgraph callgraph --tenant-id <t>                             # граф вызовов BSL (+формы/HANDLES/OVERRIDES)
uv run onec-vecgraph vectorize --tenant-id <t> [--code]                    # эмбеддинги (нужен --extra local-embeddings)
uv run onec-vecgraph search "запрос" --tenant-id <t> --mode hybrid         # поиск (+--kind/--chunk-kind/--subsystem/--expand)
uv run onec-vecgraph show Catalog.Имя --tenant-id <t> [--detail]           # карточка объекта (+полные свойства)
uv run onec-vecgraph deps Catalog.Имя --tenant-id <t>                      # зависимости
uv run onec-vecgraph ingest <manifest.yaml> --tenant-id <t>                # доки: ИТС / git-артефакты / справка
uv run onec-vecgraph ingest-help --tenant-id __shared__ --bin "<.../1cv8/8.3.x/bin>" --domain shcntx --domain shlang  # справка платформы (.hbk)
uv run onec-vecgraph platform-docinfo "Массив.Найти" --tenant-id <t>       # синтаксис-помощник: точный лукап
uv run onec-vecgraph serve --transport http                                # read-MCP: http://127.0.0.1:8000/mcp
uv run onec-lite admin                                                     # lite: живая рабочая копия + веб-админка (:8010)
```

Локальные эмбеддинги: `uv sync --extra local-embeddings` (torch cu128 под RTX 50xx),
в `.env` — `EMBEDDING_PROVIDER=local`. По умолчанию модель Qwen3-Embedding-0.6B
(для качества — `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B`). Облачные — `--extra cloud-embeddings`.

## Требования

- Python 3.12 и [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`)
- Docker + Docker Compose — для Neo4j и контейнерного деплоя (для onec-lite не нужны)

Данные Neo4j — в `./data/neo4j`, кеш модели эмбеддингов — в `./data/hf-cache` (тома compose).

## Быстрый старт

```powershell
# 1) Зависимости (создаст .venv в проекте)
uv sync

# 2) Поднять Neo4j
docker compose up -d --wait neo4j
#    Браузер Neo4j: http://localhost:7474  (neo4j / onec_vecgraph_dev)

# 3) Проверить связность
uv run onec-vecgraph health

# 4) Запустить MCP-сервер
uv run onec-vecgraph serve --transport http     # http://127.0.0.1:8000/mcp
uv run onec-vecgraph serve --transport stdio    # для локальных MCP-клиентов

# Тесты (именно tests/ — без аргумента pytest соберёт 0)
uv run pytest tests/
```

## Docker

MCP-сервер пакуется в образ; Neo4j, read-сервер (`app`) и admin-сервер поднимаются через compose.
Эмбеддинги **конфигурируемы** на сборке: CPU (по умолчанию) или GPU (cu128).

```powershell
# Сборка + запуск (MCP: http://127.0.0.1:8000/mcp, admin: :8002)
docker compose up -d --build
```

В текущем compose у сервисов `app`/`app-admin` **включён GPU** (`deploy.resources` +
`TORCH_INDEX_URL=cu128`) — нужен NVIDIA Container Toolkit; для CPU-хоста закомментируйте
блок `deploy.resources` и соберите с дефолтным (CPU) `TORCH_INDEX_URL`. Тяжёлая
индексация/векторизация запускается отдельно (CLI, обычно на GPU-хосте) — данные пишутся
в тот же Neo4j. Модель кешируется в томе `./data/hf-cache` (первый запрос качает ~1.2 ГБ).

**Аутентификация (для сетевого доступа):** включить bearer-токены —
`AUTH_ENABLED=true`, `AUTH_TOKENS="tok_abc=acme,tok_xyz=globex:ext_crm"` (токен→tenant[:config];
`X-Tenant-Id` тогда игнорируется как недоверенный), либо runtime-провижининг тенантов через
admin-сервер (`PROVISIONING_ENABLED`). Иначе разворачивать строго за аутентифицирующим gateway.
Подробности — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) и [docs/MCP_USAGE.md](docs/MCP_USAGE.md).

## Конфигурация

Все настройки — через `.env` (см. [.env.example](.env.example), каждая настройка
прокомментирована в [docs/DEPLOY_DETAILED.md](docs/DEPLOY_DETAILED.md)): Neo4j, провайдер
эмбеддингов (`hashing` для разработки, `local`, `openai`, `voyage`), MCP, auth, admin/overlay.
Настройки onec-lite — `ONEC_LITE_*` (см. [docs/LITE_USAGE.md](docs/LITE_USAGE.md)).

## Структура

```
src/onec_vecgraph/
  config.py         # настройки (pydantic-settings, .env)
  tenancy.py        # резолв арендатора: заголовки / bearer-токены
  server.py         # read-MCP (FastMCP, 29 инструментов)
  write_server.py   # overlay-write MCP (:8001, opt-in)
  admin_server.py   # admin MCP (:8002): baseline-реиндекс, провижининг, дашборд
  cli.py            # CLI: serve*/index/callgraph/vectorize/search/ingest/…
  indexer.py        # выгрузка → граф метаданных (полный/инкремент)
  callgrapher.py    # граф вызовов BSL (+формы, HANDLES, OVERRIDES)
  vectorizer.py     # чанки → эмбеддинги (мульти-вектор)
  chunking.py       # построение чанков, cAST, токенизация, точки входа
  queries.py        # поиск/граф/документы для MCP-инструментов
  ingest.py         # мультиисточник: ИТС / артефакты / справка (.hbk)
  baseline.py, jobs.py, dashboard.py, overlay*.py  # admin/overlay-контур
  parsing/          # Конфигуратор XML + parsing/edt/ (формат-агностичный шов)
  bsl/              # портируемый BSL-парсер (рутины, вызовы, аннотации)
  graph/            # схема и построение графа Neo4j
  storage/          # драйвер Neo4j (граф, векторы, doc-корпуса, callgraph)
  embeddings/       # hashing | local (Qwen3/CUDA) | cloud (+реранкер)
  sources/          # адаптеры корпусов: its, git_artifacts, hbk
  lite/             # onec-lite: MCP по живой рабочей копии (без Neo4j)
```
