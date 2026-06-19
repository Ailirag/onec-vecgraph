# onec-vecgraph — описание сервиса (точка входа для новой сессии)

Стабильная ориентация: **что это, как устроено, куда идти**. Читать первым в начале сессии.
Актуальный снимок прогресса/данных/гоч — в [STATE.md](STATE.md) (он меняется; этот файл — нет).

## Что это
MCP-сервер, который векторизует конфигурации **1С:Предприятие** (из XML-выгрузки Конфигуратора) в
**Neo4j** и отдаёт по ним поиск/граф внешним AI-агентам («роли»: BA/SA/разработчик/архитектор/
ревьюер/тимлид/тестировщик). Мультиарендный, транспорт — Streamable HTTP.

## Архитектура за 30 секунд
- **Единое хранилище — Neo4j:** граф метаданных + граф вызовов BSL + векторы (HNSW) + полнотекст.
- **Доступ — MCP, строго read-only**, мультиарендный (арендатор из заголовка/токена, не из аргументов).
  Запись (индексация/векторизация) — **только CLI** `uv run onec-vecgraph …`.
- **Поток данных (конфигурация):** XML-выгрузка → `parsing` → `graph/builder` → `indexer` (граф) →
  `callgrapher` (рутины + рёбра `CALLS`) → `vectorizer` (чанки + мульти-вектор эмбеддинги) → MCP-поиск
  (вектор + полнотекст + RRF, опц. реранкер).
- **Доп. источники:** ИТС / git-артефакты / справка платформы (`.hbk`) / БСП → `ingest` → узлы
  `:Document`/`:Artifact`, связи `MENTIONS`/`RELATES_TO`.

## Ключевые инварианты (нарушение → тихая порча данных)
1. **Ключ узла — `(tenant_id, fqn)`** (`fqn` = `<Вид>.<Имя>`). `config_id` (`base` | `ext:<имя>`) —
   это база/расширение **одной** конфигурации, **не** граница изоляции.
2. **Tenant = (организация × конфигурация).** Разные конфигурации → разные tenant. Публичная справка
   платформы/БСП → общий тенант `__shared__` (читается всеми аддитивно, скоуп формирует сервер).
3. **Одна модель/размерность эмбеддингов на БД.** Смена модели на существующей БД = полный реиндекс.

## Карта кода (`src/onec_vecgraph/`)
- `parsing/` — разбор XML-выгрузки (объекты, формы, ConfigDumpInfo); `graph/` — модель графа + схема Neo4j.
- `indexer.py` — построение графа метаданных; `callgrapher.py` + `bsl/` — граф вызовов BSL.
- `chunking.py` — построение чанков; `vectorizer.py` — эмбеддинги; `ingest.py` + `sources/` — мультиисточник.
- `embeddings/` — провайдеры (`hashing`/`local`/`cloud`) + реранкер + runtime.
- `storage/neo4j_store.py` — доступ к Neo4j; `queries.py` — поиск/граф/документы.
- `server.py` — FastMCP-сервер (21 read-only инструмент + `instructions`); `cli.py` — CLI.
- `config.py` — настройки; `tenancy.py` — резолв арендатора; `progress.py` — лог прогресса (скорость/%/ETA).

## MCP умеет (read-only, 21 инструмент; детально — [MCP_USAGE.md](MCP_USAGE.md))
здоровье/контекст (`ping`/`neo4j_health`/`whoami`) · поиск (`hybrid_search`/`semantic_search`) ·
структура (`list_metadata`/`get_object`/`get_object_properties`) · зависимости (`get_dependencies`/
`impact_analysis`/`find_type_usages`) · код (`find_handlers`/`find_callers`/`find_callees`/`call_path`) ·
документация (`find_related_docs`/`get_document`/`docinfo`) · стандарты разработки 1С
(`search_standards`/`get_standard`) · обзор (`metrics`).

## Как начать сессию (чеклист)
1. Прочитать [STATE.md](STATE.md) — актуальное состояние, что в Neo4j, ограничения, гочи.
2. Поднять Neo4j: `docker compose up -d neo4j`; проверить `uv run onec-vecgraph health`.
3. Понять задачу:
   - **управление данными** (индексация/векторизация/справка) → [OPERATOR_PLAYBOOK.md](OPERATOR_PLAYBOOK.md);
   - **потребление данных** агентом/ролью → [MCP_USAGE.md](MCP_USAGE.md);
   - **деплой/докер** → [DEPLOY_DETAILED.md](DEPLOY_DETAILED.md) / [DEPLOYMENT.md](DEPLOYMENT.md).
4. Поднять сервер: `uv run onec-vecgraph serve --transport http` (→ `http://127.0.0.1:8000/mcp`).

## Карта документации
| Документ | О чём |
|---|---|
| [STATE.md](STATE.md) | Снимок состояния: окружение, что готово, данные в Neo4j, ограничения (читать первым по сути) |
| [OPERATOR_PLAYBOOK.md](OPERATOR_PLAYBOOK.md) | Управление: index / callgraph / vectorize / ingest / ingest-help |
| [MCP_USAGE.md](MCP_USAGE.md) | Гайд для агентов-потребителей (подключение, словари, инструменты, сценарии) |
| [OVERLAY.md](OVERLAY.md) | Overlay-тенанты: baseline + per-task дельта, write-эндпоинт `index_overlay`, union-граф (Phase 2) |
| [DEPLOY_DETAILED.md](DEPLOY_DETAILED.md) · [DEPLOYMENT.md](DEPLOYMENT.md) | Деплой, докер (CPU/GPU), настройки, auth |
| [../AGENTS.md](../AGENTS.md) | Точка входа для Codex / AGENTS.md-совместимых агентов |
| [ITS_PARSER_REQUIREMENTS.md](ITS_PARSER_REQUIREMENTS.md) | Контракт внешнего парсера документации ИТС |

## Окружение
Windows + PowerShell; `uv` (`uv run [--no-sync] onec-vecgraph …`; `--no-sync` если `uv` падает на офлайн-
пересборке); Neo4j в Docker; эмбеддинги `hashing` | `local` (Qwen3-Embedding / BGE-m3) | `openai` | `voyage`;
тесты — `uv run pytest tests/` (bare `pytest` соберёт 0 — нет testpaths).
