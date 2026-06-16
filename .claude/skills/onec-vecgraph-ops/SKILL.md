---
name: onec-vecgraph-ops
description: >-
  Operate the onec-vecgraph 1C:Enterprise vectorization server — load and refresh data and manage
  its Neo4j knowledge base through the CLI (the write/operator side; the MCP itself is read-only).
  Use when the user wants to index a 1C Configurator XML dump, build the BSL call graph, vectorize /
  embed a configuration, ingest ITS or project-artifact docs, ingest platform syntax-assistant help
  (.hbk), run an incremental reindex after a config change, add a new tenant / configuration, serve
  the MCP server, or check Neo4j / index health. Triggers: «проиндексировать выгрузку»,
  «векторизировать», «обновить базу после правок», «залить справку», «поднять сервер», «реиндекс».
---

# onec-vecgraph — операторский плейбук (управление векторизацией)

Управление *записью* в базу знаний 1С. **MCP read-only** — индексацию/векторизацию запускает только CLI (`uv run onec-vecgraph …`).

**Полный плейбук (все команды, edge-cases, reset-семантика):** [docs/OPERATOR_PLAYBOOK.md](../../../docs/OPERATOR_PLAYBOOK.md) — прочитай его перед операторскими действиями. Ниже — критичное инлайн.

## Золотые правила (не нарушать)
1. **Одна модель/размерность на БД.** `EMBEDDING_PROVIDER`+`MODEL` при `vectorize`/`ingest` = как в `.env` сервера. Смена модели на существующей БД = полный реиндекс всех тенантов.
2. **Tenant = организация × конфигурация.** Разные конфигурации → разные `--tenant-id`. `config_id` (`base`|`ext:…`) — не изоляция.
3. **Справка платформы/БСП → тенант `__shared__`** (общий, читается всеми). Той же моделью, что и потребители.
4. **Секреты:** `.env` в `.gitignore` — не коммитить. Коммит/пуш — только по явной просьбе.

## Основные операции
```
# Предусловие: docker compose up -d neo4j ; uv run onec-vecgraph health
# Новая конфигурация (порядок строгий):
uv run onec-vecgraph index "<выгрузка XML>" --tenant-id acme_erp --reset
uv run onec-vecgraph callgraph --tenant-id acme_erp
uv run onec-vecgraph vectorize --tenant-id acme_erp --code
# Инкремент после правок (безопасно, по configVersion):
uv run onec-vecgraph index "<выгрузка>" --tenant-id acme_erp --incremental
uv run onec-vecgraph callgraph --tenant-id acme_erp --incremental
uv run onec-vecgraph vectorize --tenant-id acme_erp --incremental --code
# Справка платформы (.hbk) в общий тенант (путь валидируется):
uv run onec-vecgraph ingest-help --tenant-id __shared__ --bin "C:\Program Files\1cv8\8.3.27.1989\bin" --domain shcntx --domain shlang
# Doc-корпуса (ИТС/артефакты) по манифесту:
uv run onec-vecgraph ingest <manifest.yaml> --tenant-id acme_erp
# Сервер / проверка:
uv run onec-vecgraph serve --transport http
uv run onec-vecgraph metrics --tenant-id acme_erp
```
Гочи: если `uv` падает на офлайн-пересборке → `uv run --no-sync onec-vecgraph …`. Пустой результат поиска ⇒ «слой не построен для тенанта», проверь `metrics`. `vectorize --incremental` игнорирует reset (безопасно); `--no-reset --code` доливает код без перезатирания.
