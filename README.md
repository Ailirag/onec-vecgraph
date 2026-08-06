# onec-vecgraph

MCP-сервер для **векторизации конфигураций 1С:Предприятие** (XML-выгрузка Конфигуратора **и**
проекты **1C:EDT**, база + расширения) и построения **графа зависимостей** (включая граф вызовов
BSL) в **Neo4j** — граф, векторы и полнотекст в одном хранилище. Мультиарендный, read-only MCP
по Streamable HTTP; запись (индексация/векторизация) — только CLI.

## onec-lite: MCP по живой рабочей копии — без Neo4j и векторов

Для машины разработчика есть **lite-режим**: 32 инструмента (по умолчанию публикуются 23 —
см. [профили](docs/LITE_USAGE.md#состав-инструментов-профили)) по живой рабочей копии
(несколько репозиториев как именованные воркспейсы — у каждой сессии Claude Code свой)
(Конфигуратор XML **и** EDT, база + расширения), код-анализ BSL-парсером, поиск ripgrep +
опц. FTS5 (BM25), справка платформы из `.hbk`, git-осведомлённость. Запуск — двойной клик
[`lite-admin.cmd`](lite-admin.cmd) или `uv run onec-lite admin` (пути задаются в браузере).

**Рекомендуемый стандарт — один HTTP-сервер + пер-проектный воркспейс через заголовок.**
Поднимите единый долгоживущий сервис (`uv run onec-lite admin` → `http://127.0.0.1:8010/mcp`):
переживает сессии, обслуживает все проекты, FTS-индексы собираются и держатся тёплыми в фоне
(prebuild всех воркспейсов на старте). Воркспейс каждого проекта пиньте в его **project-scope
`.mcp.json`** заголовком `X-Workspace`:

```json
{ "mcpServers": { "onec-lite": {
  "type": "http", "url": "http://127.0.0.1:8010/mcp",
  "headers": { "X-Workspace": "ut" }
} } }
```

Сессия в проекте молча работает со своей конфигурацией; к другой агент обращается **только по
явной просьбе** пользователя — аргументом `workspace="<имя>"` в вызове (сильнее заголовка).
Заголовок при нескольких конфигурациях **обязателен**: без него (и без `workspace=`) сервер
**откажет** с перечнем вариантов, а не ответит по случайной — уверенный ответ про чужую
конфигурацию хуже отказа. `list_workspaces()` работает без выбора, так что выход из отказа есть
всегда; одна настроенная конфигурация не требует ничего.
Альтернатива для разовой привязки — stdio per-session (`claude mcp add … onec-lite --workspace <имя>`);
на больших конфигурациях FTS может не успеть собраться за сессию. Порт — `ONEC_LITE_PORT` (дефолт 8010).
**Руководство: [docs/LITE_USAGE.md](docs/LITE_USAGE.md).**

### Эти инструменты не заменяют ripgrep

Правило: нужен **разбор** BSL/метаданных 1С — MCP; нужен **текст** или **счёт вхождений** — rg.
По токенам компетентный rg дешевле в 2–5 раз (на агрегатах через конвейер — в 10–25), потому что
отдаёт `путь:строка:текст` без обвязки; выигрыш MCP — в проверенности и полноте ответа.

| Задача | Чем |
|---|---|
| Вызов это или объявление; полный счёт вызовов | `find_callers` / `call_graph` / `find_callees` |
| Реквизиты с типами и обязательностью, движения — со слиянием базы и расширений | `get_object` / `writes_to` |
| Кому принадлежит перехват расширения | `find_overrides` |
| «Где считается X» человеческим языком | `fts_search` (BM25) |
| Дифф → затронутые рутины → их вызывающие | `review_set` |
| Сколько чего и по каким объектам (агрегаты) | `bsl_sql` — один `GROUP BY` по индексу |
| Текст, подстрока, regex | `rg -n "<regex>" <корни>` |
| Сколько всего и по каким объектам | `rg -c … \| sort \| uniq -c` (×10–25 дешевле) |
| Что изменилось в ветке | `git diff --name-status` (×5 дешевле) |

Поэтому профиль по умолчанию (`lean`, 23 инструмента) не публикует `search_code`,
`search_metadata`, `list_routines`, `read_file`/`read_module`, `changed_objects` — это не потеря,
а замена; профиль `full` возвращает их, если у агента нет шелла. Корни для rg берите из
`overview()`: база и каждое расширение — отдельный корень, иначе теряется до 75 % результатов.
Полная карта с замерами — [docs/LITE_USAGE.md#rg-или-mcp-карта-маршрутизации](docs/LITE_USAGE.md#rg-или-mcp-карта-маршрутизации).

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
