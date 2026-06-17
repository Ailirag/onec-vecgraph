# AGENTS.md — onec-vecgraph

Инструкции для AI-агентов (OpenAI Codex и совместимых, читающих `AGENTS.md`).

## Что это за репозиторий
`onec-vecgraph` — Python MCP-сервер: векторизует конфигурации 1С:Предприятие (из XML-выгрузки Конфигуратора)
в Neo4j (единое хранилище: граф метаданных + граф вызовов BSL + векторы + полнотекст), мультиарендный,
по Streamable HTTP. Обслуживает внешних AI-агентов («роли»: BA/SA/разработчик/архитектор/ревьюер/тимлид/тестировщик).

- **MCP-сервер — строго read-only**: только отвечает на вопросы о конфигурации, ничего не меняет.
- **Запись/управление (индексация, векторизация, загрузка справки) — только через CLI** `uv run onec-vecgraph …`.
- Окружение: Windows + PowerShell; `uv` для запуска; Neo4j через `docker compose`.

## Операторские задачи (управление векторизацией)
**Полный плейбук — [docs/OPERATOR_PLAYBOOK.md](docs/OPERATOR_PLAYBOOK.md). Открой его перед действиями с записью.**
Критичное инлайн:

### Золотые правила (нарушение → тихая порча данных)
1. **Одна модель/размерность эмбеддингов на БД.** `EMBEDDING_PROVIDER`+`EMBEDDING_MODEL` при `vectorize`/`ingest`
   обязаны совпадать с `.env` работающего сервера. Смена модели на существующей БД = полный реиндекс всех тенантов.
2. **Tenant = (организация × конфигурация).** Разные конфигурации → разные `--tenant-id` (`acme_erp`, `acme_ut`).
   `config_id` (`base`|`ext:<имя>`) — это база/расширение одной конфигурации, НЕ граница изоляции. Две конфигурации
   в одном тенанте схлопнутся по `fqn`.
3. **Справка платформы/БСП → общий тенант `__shared__`** (читается всеми аддитивно), эмбеддить той же моделью.
4. **Секреты:** `.env` в `.gitignore` — не коммитить. **Коммит/пуш — только по явной просьбе пользователя.**

### Основные команды
```
# Предусловие: docker compose up -d neo4j ; uv run onec-vecgraph health
# Новая конфигурация (порядок строгий index → callgraph → vectorize):
uv run onec-vecgraph index "<выгрузка XML>" --tenant-id acme_erp --reset
uv run onec-vecgraph callgraph --tenant-id acme_erp
uv run onec-vecgraph vectorize --tenant-id acme_erp --code
# Инкремент после правок (безопасно, по configVersion):
uv run onec-vecgraph index "<выгрузка>" --tenant-id acme_erp --incremental
uv run onec-vecgraph callgraph --tenant-id acme_erp --incremental
uv run onec-vecgraph vectorize --tenant-id acme_erp --incremental --code
# Справка платформы (.hbk) → общий тенант (путь валидируется до старта):
uv run onec-vecgraph ingest-help --tenant-id __shared__ --bin "C:\Program Files\1cv8\8.3.27.1989\bin" --domain shcntx --domain shlang
# Doc-корпуса ИТС/артефакты по манифесту:
uv run onec-vecgraph ingest <manifest.yaml> --tenant-id acme_erp
# Сервер / диагностика:
uv run onec-vecgraph serve --transport http
uv run onec-vecgraph serve-write --transport http   # overlay write-эндпоинт (нужен OVERLAY_WRITE_ENABLED=true)
uv run onec-vecgraph metrics --tenant-id acme_erp
```
Overlay (baseline + per-task дельта разработчика): `serve-write`/`index-overlay` пишет только в `<base>@task/*`; графовые инструменты принимают `overlay_tenant_id` для union-чтения. Детали — [docs/OVERLAY.md](docs/OVERLAY.md).
Гочи: при падении `uv` на офлайн-пересборке → `uv run --no-sync onec-vecgraph …`. `vectorize --incremental` игнорирует
reset (безопасно); `--no-reset --code` доливает код без перезатирания. Пустой результат поиска ⇒ «слой не построен
для тенанта» — проверь `metrics` (есть ли chunks/routines).

Классификация для фильтрованного поиска (owner-фасеты): `index --config-release "ERP_2.5.18"` → `corpus_version=config:<релиз>`
на объектах; источники манифеста (`its`/`git_artifacts`) принимают `doc_topic` (`platform`/`config`/`task`) и `corpus_version`.
Потребитель фильтрует по `doc_topic`/`corpus_version`/`help_kind` — только вместе с соответствующим `source`. Изоляцию это
НЕ заменяет (только тенант). Детали — [docs/OPERATOR_PLAYBOOK.md](docs/OPERATOR_PLAYBOOK.md), [docs/MCP_USAGE.md](docs/MCP_USAGE.md).

## Прочее
- Тесты: `uv run pytest tests/` (bare `pytest` соберёт 0 — нет testpaths).
- Как роли-потребители читают данные (read-only API из 19 инструментов) — [docs/MCP_USAGE.md](docs/MCP_USAGE.md).
- Снимок состояния проекта и инварианты — [docs/STATE.md](docs/STATE.md).
