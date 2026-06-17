# Пошаговый runbook развёртывания `onec-vecgraph`

Практический чеклист «с нуля до работающего сервера на несколько конфигураций», с настройками на
каждом этапе. Подробности и обоснования — [DEPLOYMENT.md](DEPLOYMENT.md); вызов инструментов —
[MCP_USAGE.md](MCP_USAGE.md). Команды — для хоста с Docker (пути монтирования в примерах под Windows;
на Linux замените на POSIX-пути).

---

## Этап 0. Предусловия и выбор профиля

Профиль эмбеддингов определяет, какой образ собирать и какие настройки задавать дальше.

| Профиль | Когда | Образ (build-arg) | Ключевые настройки |
|---|---|---|---|
| **CPU local** | без GPU, качество Qwen3, данные не уходят наружу | `EXTRAS=local-embeddings`, `TORCH_INDEX_URL=…/cpu` (дефолт) | `EMBEDDING_PROVIDER=local`, `EMBEDDING_DEVICE=cpu` |
| **GPU local** | есть NVIDIA GPU, тяжёлая векторизация на месте | `TORCH_INDEX_URL=…/cu128` | `EMBEDDING_DEVICE=cuda` + `--gpus all` |
| **Cloud** | без GPU, можно слать тексты в API | `EXTRAS=cloud-embeddings` (без torch) | `EMBEDDING_PROVIDER=openai\|voyage` + ключ |

Нужно: Docker + Docker Compose; для GPU — NVIDIA Container Toolkit; ~2 ГБ под модель (local) или
API-ключ (cloud).

---

## Этап 1. Файл окружения `.env`

Скопируйте `.env.example` → `.env`. Значимые блоки:

```dotenv
# Neo4j (в проде — НЕ дефолтный пароль)
NEO4J_PASSWORD=<надёжный-пароль>

# Эмбеддинги — ОДИН профиль, одинаковый для индексации и для запросов!
EMBEDDING_PROVIDER=local            # local | openai | voyage
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_DEVICE=cpu                # cpu | cuda | auto  (только local)
# cloud вместо двух строк выше:
# EMBEDDING_PROVIDER=openai
# EMBEDDING_MODEL=text-embedding-3-large
# OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=                  # для OpenAI-совместимого шлюза (Azure/прокси)
# VOYAGE_API_KEY=...                # если voyage

# Изоляция арендаторов
REQUIRE_TENANT=true

# Аутентификация (рекомендуется для сетевого доступа)
AUTH_ENABLED=true
AUTH_TOKENS=tok_erp=erp,tok_ut=ut   # токен=tenant  или  токен=tenant:config
```
Сгенерировать стойкий клиентский токен: `python -c "import secrets; print('tok_'+secrets.token_urlsafe(32))"`.
Выпуск всех ключей/доступов (bearer-токены, OpenAI/Voyage, git deploy key для источников, HF_TOKEN) и
их хранение — [DEPLOYMENT.md §10](DEPLOYMENT.md).

> **Критичное правило:** `EMBEDDING_PROVIDER`+`EMBEDDING_MODEL` при индексации и при работе сервиса
> должны совпадать — размерность векторного индекса фиксируется при `vectorize`.

`MCP_HOST/PORT` и `NEO4J_URI` для compose уже заданы в образе (`0.0.0.0:8000`, `bolt://neo4j:7687`) —
менять не нужно.

---

## Этап 2. Сборка образа и поднятие Neo4j + сервера

```powershell
# CPU local (дефолт)
docker compose up -d --build

# GPU local: cu128 + cuda (+ раскомментировать deploy.resources в docker-compose.yml)
$env:TORCH_INDEX_URL="https://download.pytorch.org/whl/cu128"; $env:EMBEDDING_DEVICE="cuda"
docker compose up -d --build

# Cloud: лёгкий образ без torch
$env:EXTRAS="cloud-embeddings"; docker compose up -d --build
```

Поднимается `neo4j` (heap 8G/pagecache 4G, данные в `./data/neo4j`), затем `app` (ждёт `neo4j`
healthy). Проверка:
```powershell
docker compose ps                       # оба healthy
docker compose logs app --tail 20
```

**Опционально — overlay-write эндпоинт** (per-task дельта разработчика, нужен оркестратору; см.
[DEPLOYMENT.md §4.1](DEPLOYMENT.md) и [OVERLAY.md](OVERLAY.md)). По умолчанию НЕ поднимается — деплой
read-only. Включается профилем `overlay-write` (поднимет ещё сервис `app-write` на порту 8001):
```powershell
# в .env: WRITE_AUTH_TOKENS=wtok=grand-dev-mdm@release   # token=base (запись только '<base>@task/*')
docker compose --profile overlay-write up -d --build
docker compose ps                       # neo4j + app + app-write healthy
```

---

## Этап 3. Офлайн-индексация каждой конфигурации

Выгрузка монтируется как **runtime-том** (в образ не входит). По одному циклу на конфигурацию;
родительский каталог удобно примонтировать один раз.

```powershell
# 3.1 граф метаданных (+WRITES_TO, +:Detail)
docker compose run --rm -v C:\1C\xml:/dumps:ro app `
  onec-vecgraph index /dumps/ERP_UH --tenant-id erp --reset

# 3.2 граф вызовов BSL (рутины/CALLS/HANDLES/entry_points/менеджерные)
docker compose run --rm app onec-vecgraph callgraph --tenant-id erp

# 3.3 векторизация (+код); тем же провайдером/моделью, что и сервис
docker compose run --rm app onec-vecgraph vectorize --tenant-id erp --code
```

Настройки этапа:
- `--tenant-id` — раздельный на каждую конфигурацию/компанию (полная изоляция). База+расширения одной
  конфигурации идут в один tenant с разными `config_id` (парсер проставляет сам).
- `--reset` — пересоздать граф (первый прогон). Далее обновления — `--incremental` на всех трёх командах.
- `EMBEDDING_BATCH_SIZE=64` (env) — ускоряет векторизацию на масштабе; `EMBEDDING_MAX_SEQ_LENGTH=256`
  — ограничивает VRAM.
- `vectorize --code` тяжёлый (часы на ERP-масштабе); для local разумно гонять на **GPU-образе**, затем
  сервить на CPU/cloud.

Повторите 3.1–3.3 для остальных (`/dumps/UT --tenant-id ut`, …).

### 3.4. Корпуса документации (опц.): ИТС + проектные артефакты
Если есть ИТС/проектные доки — описать их в манифесте (`sources.example.yaml`) и заингестить:
```powershell
docker compose run --rm -v C:\path\sources.yaml:/m.yaml:ro app `
  onec-vecgraph ingest /m.yaml --tenant-id erp [--only its|git_artifacts] [--reset] [--link-semantic]
```
git-репо источников требуют доступа (deploy key / PAT) — см. [DEPLOYMENT.md §10.4](DEPLOYMENT.md).
Детали — [DEPLOYMENT.md §5.2](DEPLOYMENT.md).

### 3.5. Справка платформы (синтаксис-помощник) — опц., в общий тенант
Запуск **только из CLI** (MCP read-only). Команда `ingest-help` сама **проверяет путь** к `.hbk`:
```powershell
docker compose run --rm -v "C:\Program Files\1cv8\8.3.27.1989\bin:/pf-bin:ro" app `
  onec-vecgraph ingest-help --tenant-id __shared__ --bin /pf-bin --domain shcntx --domain shlang [--reset]
```
Если путь не задан/`.hbk` не найден — ошибка и `exit 1` (а не тихий 0). Версия берётся из пути `bin`
(или `--platform-version`); грузится один раз на сборку. Детали — [DEPLOYMENT.md §5.3](DEPLOYMENT.md).

---

## Этап 4. Смоук-проверка

```powershell
# инвентарь по tenant — подтверждает, что слои построены
docker compose run --rm app onec-vecgraph metrics --tenant-id erp
```
Через MCP-клиент вызвать `whoami` (привязка tenant), `neo4j_health`, `hybrid_search` с тестовым
запросом. Пустые результаты поиска/графа вызовов ⇒ соответствующий слой не построен — вернитесь к этапу 3.

Конфиг MCP-клиента (режим `AUTH_ENABLED=true`):
```json
{ "mcpServers": { "onec-vecgraph": {
  "url": "https://<host>/mcp",
  "headers": { "Authorization": "Bearer tok_erp" }
}}}
```

---

## Этап 5. Прод-хардненинг (перед публичным доступом)

| Что | Настройка/действие |
|---|---|
| Аутентификация | `AUTH_ENABLED=true`, `AUTH_TOKENS` из секрета (docker/k8s secret), не в git |
| TLS/доступ | reverse-proxy (nginx/traefik) с TLS перед `app`; порт `app` не публиковать наружу (слушает `127.0.0.1`) |
| Neo4j | `NEO4J_PASSWORD` не дефолтный; бэкап тома `./data/neo4j` |
| Модель (local) | постоянный том `./data/hf-cache:/models` (уже в compose) или запечь модель в образ |
| Масштаб | сервер `stateless_http` → несколько реплик `app` за балансировщиком, общий Neo4j |
| Обновления | периодический `index\|vectorize\|callgraph --incremental` на изменённых выгрузках |

---

## Шпаргалка: значимые переменные

| Переменная | Значения / пример | Этап |
|---|---|---|
| `NEO4J_PASSWORD` | надёжный пароль | 1 |
| `EMBEDDING_PROVIDER` | `local` \| `openai` \| `voyage` \| `hashing` | 1 |
| `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-0.6B` \| `text-embedding-3-large` \| `voyage-3` | 1 |
| `EMBEDDING_DEVICE` | `cpu` \| `cuda` \| `auto` (local) | 1–2 |
| `EMBEDDING_DIMENSIONS` | опц., cloud, ≤4096 | 1 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `VOYAGE_API_KEY` | ключи cloud | 1 |
| `EMBEDDING_BATCH_SIZE` | `64` на масштабе | 3 |
| `REQUIRE_TENANT` | `true` | 1 |
| `AUTH_ENABLED` / `AUTH_TOKENS` | `true` / `tok=tenant[:config],…` | 1,5 |
| `EXTRAS` (build) | `local-embeddings` \| `cloud-embeddings` | 2 |
| `TORCH_INDEX_URL` (build) | `…/whl/cpu` \| `…/whl/cu128` | 2 |
