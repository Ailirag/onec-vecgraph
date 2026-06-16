---
mode: agent
description: "onec-vecgraph — операторский плейбук: индексация / callgraph / векторизация / загрузка справки / запуск сервера 1C-векторбазы (write-сторона; MCP read-only)."
---

# onec-vecgraph — операторский плейбук (управление векторизацией)

Управляешь *записью* в базу знаний 1С (Neo4j: граф + векторы + полнотекст). **MCP-сервер read-only** —
индексацию/векторизацию запускает только CLI `uv run onec-vecgraph …`.

**Полный плейбук (все команды, edge-cases, reset-семантика):** [docs/OPERATOR_PLAYBOOK.md](../../docs/OPERATOR_PLAYBOOK.md) —
прочитай его перед операторскими действиями. Ниже — критичное инлайн.

## Золотые правила (нарушение → тихая порча данных)
1. **Одна модель/размерность эмбеддингов на БД.** `EMBEDDING_PROVIDER`+`EMBEDDING_MODEL` при `vectorize`/`ingest`
   обязаны совпадать с `.env` работающего сервера. Смена модели на существующей БД = полный реиндекс всех тенантов.
2. **Tenant = (организация × конфигурация).** Разные конфигурации → разные `--tenant-id`. `config_id` (`base`|`ext:…`) — не изоляция.
3. **Справка платформы/БСП → общий тенант `__shared__`** (читается всеми аддитивно), эмбеддить той же моделью.
4. **Секреты:** `.env` в `.gitignore` — не коммитить. Коммит/пуш — только по явной просьбе пользователя.

## Основные команды
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
uv run onec-vecgraph metrics --tenant-id acme_erp
```
Гочи: при падении `uv` на офлайн-пересборке → `uv run --no-sync onec-vecgraph …`. `vectorize --incremental` игнорирует
reset (безопасно); `--no-reset --code` доливает код без перезатирания. Пустой результат поиска ⇒ «слой не построен
для тенанта» — проверь `metrics`.

Классификация для фильтрованного поиска (owner-фасеты): `index --config-release "ERP_2.5.18"` → `corpus_version=config:<релиз>`
на объектах; источники манифеста (`its`/`git_artifacts`) принимают `doc_topic` (`platform`/`config`/`task`) и `corpus_version`.
Потребитель фильтрует по `doc_topic`/`corpus_version`/`help_kind` — только вместе с соответствующим `source`. Изоляцию это
НЕ заменяет (только тенант). Детали — [docs/OPERATOR_PLAYBOOK.md](../../docs/OPERATOR_PLAYBOOK.md), [docs/MCP_USAGE.md](../../docs/MCP_USAGE.md).
