# onec-vecgraph

MCP-сервер для **векторизации конфигураций 1С:Предприятие** (из XML-выгрузки Конфигуратора, НЕ EDT)
и построения **графа зависимостей** (включая граф вызовов BSL) в **Neo4j**.

Полный план и архитектура: [PLAN.md](PLAN.md).
**Как вызывать MCP сторонним агентам/ролям** (подключение, заголовки, fqn, словари, карта инструментов,
сценарии): [docs/MCP_USAGE.md](docs/MCP_USAGE.md).
**Деплой в Docker и варианты образа** (CPU/GPU/cloud, env, аутентификация, офлайн-индексация):
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). **Пошаговый runbook развёртывания**:
[docs/DEPLOY_RUNBOOK.md](docs/DEPLOY_RUNBOOK.md).

## Статус

- **M0** — каркас (конфиг, мультиарендность, MCP-сервер HTTP+stdio, Neo4j) ✓
- **M1 / M1.1** — парсер XML Конфигуратора + граф метаданных (объекты, реквизиты, типы, ТЧ,
  перечисления, предопределённые, подсистемы, права, подписки, владельцы) ✓
  Проверено на УТ (14k объектов) и ERP/УХ (24k объектов), 0 ошибок парсинга.
- **M2 / M2.1** — векторизация метаданных + поиск (`semantic_search`, `hybrid_search`):
  мульти-вектор (имя × смысл) + полнотекст, объединение RRF; чанки объектов/реквизитов/ТЧ/
  значений перечислений/предопределённых/**форм** (заголовки и подписи элементов из Form.xml)/
  **кода модулей** (per-routine, объектные/общие/форм; флаг `vectorize --code`).
  Локальные эмбеддинги Qwen3-Embedding на CUDA
  (RTX 50xx, torch cu128); опц. реранкер. ✓
- **M3** — граф вызовов BSL (`find_callers`, `find_callees`, `call_path`); портируемый
  парсер процедур/функций и вызовов, рёбра `CALLS` (local + общие модули). Модули форм —
  рутины + `CALLS` + `HANDLES` (событие формы → обработчик). ✓
- **Мультиарендность по HTTP** — tenant/config из заголовков `X-Tenant-Id` / `X-Config-Id`,
  изоляция по компании (без заголовка запрос отклоняется). ✓
- **Инкрементальность** — по хешам `configVersion` из `ConfigDumpInfo.xml` обновляются только
  изменённые/удалённые объекты: граф метаданных, векторизация и граф вызовов BSL
  (`index|vectorize|callgraph --incremental`). Входящие связи сохраняются. ✓
- Проверено на УТ (≈14,8k объектов) и ERP/УХ (≈23,9k объектов).
- Дальше — точность разрешения вызовов BSL (менеджерные вызовы), реранкер по умолчанию.

### Команды

```powershell
uv run onec-vecgraph index "<путь к выгрузке>" --tenant-id <t> --reset        # построить граф
uv run onec-vecgraph index "<путь к выгрузке>" --tenant-id <t> --incremental  # только изменённые
uv run onec-vecgraph vectorize --tenant-id <t>                            # эмбеддинги (нужен --extra local-embeddings)
uv run onec-vecgraph search "запрос" --tenant-id <t> --mode hybrid        # поиск
uv run onec-vecgraph show Catalog.Имя --tenant-id <t>                     # карточка объекта
uv run onec-vecgraph deps Catalog.Имя --tenant-id <t>                     # зависимости
```

Локальные эмбеддинги: `uv sync --extra local-embeddings` (torch cu128 под RTX 50xx),
в `.env` — `EMBEDDING_PROVIDER=local`. По умолчанию модель Qwen3-Embedding-0.6B
(для качества — `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B`).

## Требования

- Python 3.12 (управляется через `uv`)
- `uv` (установлен в `D:\tools\uv`)
- Docker + Docker Compose (для Neo4j)

Всё хранится на диске **D** (venv в проекте, кеш uv и managed-Python в `D:\tools\uv`,
данные Neo4j в `./data/neo4j`).

## Быстрый старт

```powershell
# 1) Зависимости (создаст .venv на D, поставит Python 3.12)
uv sync

# 2) Поднять Neo4j (данные → ./data/neo4j на диске D)
docker compose up -d --wait
#    Браузер Neo4j: http://localhost:7474  (neo4j / onec_vecgraph_dev)

# 3) Проверить связность
uv run onec-vecgraph health

# 4) Запустить MCP-сервер
uv run onec-vecgraph serve --transport http     # http://127.0.0.1:8000/mcp
uv run onec-vecgraph serve --transport stdio    # для локальных MCP-клиентов

# Тесты
uv run pytest -q
```

## Docker

MCP-сервер пакуется в образ; Neo4j и сервер поднимаются вместе через compose.
Эмбеддинги **конфигурируемы** на сборке: CPU (по умолчанию) или GPU (cu128).

```powershell
# CPU-образ + Neo4j (сервер эмбеддит только запрос — на CPU достаточно)
docker compose up -d --build                       # MCP: http://127.0.0.1:8000/mcp

# GPU-образ (нужен NVIDIA Container Toolkit; раскомментировать deploy.resources в compose):
$env:TORCH_INDEX_URL="https://download.pytorch.org/whl/cu128"; $env:EMBEDDING_DEVICE="cuda"
docker compose up -d --build
```

Тяжёлая индексация/векторизация (`index`/`vectorize`/`callgraph`) запускается отдельно (CLI,
обычно на GPU-хосте) — данные пишутся в тот же Neo4j. Модель эмбеддингов кешируется в томе
`./data/hf-cache` (первый запрос качает ~1.2 ГБ).

**Аутентификация (для сетевого доступа):** включить bearer-токены —
`AUTH_ENABLED=true`, `AUTH_TOKENS="tok_abc=acme,tok_xyz=globex:ext_crm"` (токен→tenant[:config];
`X-Tenant-Id` тогда игнорируется как недоверенный). Иначе разворачивать строго за
аутентифицирующим gateway. Подробности вызова — [docs/MCP_USAGE.md](docs/MCP_USAGE.md).

## Конфигурация

Все настройки — через `.env` (см. [.env.example](.env.example)): подключение к Neo4j,
провайдер эмбеддингов (`hashing` для разработки, `local`/`openai` далее), параметры MCP,
дефолтный tenant-контекст.

## Структура

```
src/onec_vecgraph/
  config.py        # настройки (pydantic-settings)
  tenancy.py       # контекст арендатора (tenant_id / config_id)
  server.py        # MCP-сервер (FastMCP): ping, neo4j_health
  cli.py           # CLI: serve / health / index / version
  storage/         # обёртка Neo4j
  embeddings/      # провайдеры эмбеддингов (hashing | local | cloud)
  parsing/         # парсер XML Конфигуратора        (M1)
  bsl/             # анализ BSL и граф вызовов        (M3)
  graph/           # построение графа в Neo4j         (M1)
```
