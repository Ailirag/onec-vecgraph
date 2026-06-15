# Детальная инструкция по запуску onec-vecgraph (с пояснением каждой настройки)

Пошаговый разбор развёртывания, где **каждая настройка прокомментирована** (что делает, значение по
умолчанию, когда менять). Краткий чеклист — [DEPLOY_RUNBOOK.md](DEPLOY_RUNBOOK.md); обоснования и
разделы прод-эксплуатации — [DEPLOYMENT.md](DEPLOYMENT.md); вызов инструментов — [MCP_USAGE.md](MCP_USAGE.md).

Команды даны для Docker. Пути с пробелами — в кавычках. На Windows — PowerShell/Git Bash.

---

## Шаг 0. Выбрать профиль эмбеддингов

От профиля зависит, какой образ собирать и какие переменные задавать. Выберите один:

| Профиль | Когда выбирать | Образ (build-arg) | Рантайм |
|---|---|---|---|
| **CPU-local** (по умолчанию) | нет GPU; данные не должны уходить наружу; нужна офлайн-работа | `EXTRAS=local-embeddings`, `TORCH_INDEX_URL=…/cpu` | `EMBEDDING_PROVIDER=local`, `EMBEDDING_DEVICE=cpu` |
| **GPU-local** | есть NVIDIA GPU; тяжёлая векторизация на месте | `TORCH_INDEX_URL=…/cu128` | `EMBEDDING_DEVICE=cuda` + `--gpus all` |
| **Cloud** | нет GPU; допустимо слать тексты в облако | `EXTRAS=cloud-embeddings` (без torch) | `EMBEDDING_PROVIDER=openai\|voyage` + ключ |

Предусловия: **Docker + Docker Compose**; для GPU — **NVIDIA Container Toolkit**; ~2 ГБ диска под модель
(local) или API-ключ (cloud); доступ к каталогу с XML-выгрузкой конфигурации 1С.

---

## Шаг 1. Получить код и подготовить `.env`

```bash
git clone https://github.com/Ailirag/onec-vecgraph.git
cd onec-vecgraph
cp .env.example .env          # далее правим .env (Шаг 2)
```
`.env` **в `.gitignore`** — секреты в репозиторий не попадают; в Docker-образ тоже не копируется.

---

## Шаг 2. `.env` — каждая переменная с комментарием

> Все эти ключи читаются `pydantic-settings` (без префикса, регистр не важен). В Docker те же значения
> прокидываются через `docker-compose.yml` (Шаг 3); локальный CLI читает их из `.env`.

```dotenv
# ── Neo4j (граф + векторы + полнотекст в одном хранилище) ───────────────────
NEO4J_URI=bolt://localhost:7687   # адрес Neo4j. Локальный CLI → localhost; в compose app сам выставляет
                                  # bolt://neo4j:7687 (имя сервиса). Менять при внешнем/удалённом Neo4j.
NEO4J_USER=neo4j                  # пользователь БД. По умолчанию neo4j.
NEO4J_PASSWORD=onec_vecgraph_dev  # ПАРОЛЬ. Дефолт — только для локалки. В проде ОБЯЗАТЕЛЬНО сменить
                                  # (compose прокидывает его и в neo4j, и в app).
NEO4J_DATABASE=neo4j              # имя БД. На Community одна БД 'neo4j'. Меняют при db-per-tenant (Enterprise).

# ── Эмбеддинги (один профиль; ОДИНАКОВЫЙ при индексации и при запросах!) ─────
EMBEDDING_PROVIDER=local          # hashing | local | openai | voyage.
                                  #   hashing — без ML (детермин. хеш), только для dev/тестов;
                                  #   local   — sentence-transformers на CPU/GPU (нужен extra local-embeddings);
                                  #   openai/voyage — облако (нужен extra cloud-embeddings + ключ).
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B  # модель. local: Qwen3-0.6B (1024-dim, дефолт), или -4B (2560, качество),
                                  # или BAAI/bge-m3. openai: text-embedding-3-large/-small. voyage: voyage-3.
EMBEDDING_DEVICE=cpu              # local: cpu | cuda | auto (auto = cuda если доступна). Для GPU-профиля — cuda.
EMBEDDING_BATCH_SIZE=16           # размер батча эмбеддинга. На масштабе (ERP) поднимать до 32–64 (быстрее,
                                  # больше VRAM/RAM).
EMBEDDING_MAX_SEQ_LENGTH=256      # макс. токенов на чанк. Ограничивает VRAM (внимание ~O(n^2)) и ускоряет.
                                  # Увеличивать только если чанки реально длиннее и есть память.
EMBEDDING_DIM=256                 # размерность ТОЛЬКО для провайдера hashing. Для local/cloud игнорируется
                                  # (берётся из модели).
# EMBEDDING_DIMENSIONS=1024       # cloud-only: усечь размерность (OpenAI 3-*, Voyage 3-large/3.5); ≤4096. Пусто = дефолт модели.
# OPENAI_API_KEY=sk-...           # ключ OpenAI (provider=openai).
# OPENAI_BASE_URL=https://gw/v1   # OpenAI-совместимый шлюз (Azure/прокси/self-hosted).
# VOYAGE_API_KEY=...              # ключ Voyage (provider=voyage).
# HF_TOKEN=hf_...                 # опц.: токен HuggingFace — снимает rate-limit при скачивании local-модели.

# Реранкер (опц., по умолчанию выключен; качает ~2 ГБ cross-encoder и требует torch):
# RERANK_ENABLED=false            # true → пост-ранжирование топа cross-encoder'ом (точнее, медленнее).
# RERANK_MODEL=BAAI/bge-reranker-v2-m3

# ── MCP-сервер (Streamable HTTP) ────────────────────────────────────────────
MCP_HOST=127.0.0.1   # интерфейс прослушивания. Локально 127.0.0.1; в контейнере app сам ставит 0.0.0.0.
MCP_PORT=8000        # порт HTTP.
MCP_PATH=/mcp        # путь эндпоинта MCP.

# ── Арендатор по умолчанию (для stdio/CLI; по HTTP берётся из заголовка/токена) ─
REQUIRE_TENANT=true       # по HTTP БЕЗ X-Tenant-Id (или токена) — запрос отклоняется (изоляция компаний).
                          # false разрешает дефолтный tenant без заголовка — только для локалки.
DEFAULT_TENANT_ID=default # tenant по умолчанию (stdio/CLI и при REQUIRE_TENANT=false).
DEFAULT_CONFIG_ID=base    # config по умолчанию (base | ext:<имя расширения>).

# ── Общий публичный тенант (справка платформы/БСП — общая для всех) ─────────
SHARED_TENANT_ID=__shared__   # зарезервированный tenant с публичными корпусами. Поиск/docinfo читают его
                              # АДДИТИВНО к tenant клиента; список тенантов формирует сервер (не клиент).
INCLUDE_SHARED_TENANT=true    # false — отключить аддитивное чтение общего тенанта.

# ── Аутентификация HTTP (рекомендуется для сетевого доступа) ────────────────
AUTH_ENABLED=false        # true → каждый HTTP-вызов требует 'Authorization: Bearer <token>'; tenant берётся
                          # из карты ниже, а НЕ из X-Tenant-Id (подделать нельзя). false → доверенный
                          # X-Tenant-Id (только за аутентифицирующим gateway / в локалке).
# AUTH_TOKENS=tok_a=acme,tok_b=globex:ext_crm   # карта токен=tenant[:config]. По токену на потребителя
                          # (удобно отзывать). Генерация: python -c "import secrets;print('tok_'+secrets.token_urlsafe(32))"
```

> **Главное правило эмбеддингов:** `EMBEDDING_PROVIDER`+`EMBEDDING_MODEL` при `index/vectorize/ingest` и
> при работе сервера должны **совпадать** — размерность векторного индекса фиксируется при первой
> векторизации; смешение моделей/размерностей ломает поиск (индекс один на всю БД).

---

## Шаг 3. `docker-compose.yml` — что делает каждая настройка

Файл уже готов; ниже — смысл ключевых строк, чтобы осознанно править.

**Сервис `neo4j`:**
```yaml
ports: ["127.0.0.1:7474:7474", "127.0.0.1:7687:7687"]  # 7474 — браузер Neo4j, 7687 — Bolt (драйвер).
                                                        # Слушает только loopback — наружу не торчит.
environment:
  NEO4J_AUTH: "neo4j/${NEO4J_PASSWORD:-onec_vecgraph_dev}"     # логин/пароль БД (из .env).
  NEO4J_server_memory_heap_max__size: "8G"   # JVM heap. Под ERP-масштаб 8G; уменьшать на слабой машине.
  NEO4J_server_memory_pagecache_size: "4G"   # кэш страниц (горячие данные графа/индексов в RAM).
  NEO4J_dbms_memory_transaction_total_max: "6G"  # потолок памяти на ВСЕ транзакции (под крупные bat-записи).
  NEO4J_db_memory_transaction_max: "4G"      # потолок на ОДНУ транзакцию.
volumes: [./data/neo4j/...]   # данные/логи/плагины на диск (переживают перезапуск). Это бэкап-цель.
healthcheck: cypher-shell ... # app стартует только когда Neo4j healthy (depends_on ниже).
```

**Сервис `app` (MCP-сервер):**
```yaml
build.args.EXTRAS: local-embeddings        # что ставить в образ: local-embeddings (torch+ST) | cloud-embeddings.
build.args.TORCH_INDEX_URL: .../whl/cpu     # индекс torch: cpu (по умолч.) или .../whl/cu128 (GPU).
depends_on.neo4j.condition: service_healthy # не стартовать app, пока БД не готова.
ports: ["127.0.0.1:8000:8000"]              # MCP на loopback. Наружу — только через TLS-прокси (Шаг 10).
environment:
  MCP_HOST: 0.0.0.0          # внутри контейнера слушаем все интерфейсы (наружу всё равно проксируем).
  NEO4J_URI: bolt://neo4j:7687  # адрес БД = имя сервиса compose.
  NEO4J_PASSWORD / EMBEDDING_* / OPENAI_* / VOYAGE_* / HF_* / REQUIRE_TENANT /
  SHARED_TENANT_ID / INCLUDE_SHARED_TENANT / AUTH_ENABLED / AUTH_TOKENS  # см. Шаг 2 (берутся из .env).
volumes: ["./data/hf-cache:/models"]  # кэш скачанной local-модели (HF_HOME=/models) между перезапусками.
# deploy.resources.reservations.devices [gpu]  # РАСКОММЕНТировать для GPU-образа + хост с NVIDIA toolkit.
```

---

## Шаг 4. Собрать образ (build-args разобраны)

> ⚠️ Форма `VAR=значение docker compose …` — это **bash**; в Windows (CMD/PowerShell) она НЕ работает.
> Ниже — кросс-платформенный способ через `--build-arg` (работает в CMD/PowerShell/bash).

```bash
# CPU-local (по умолчанию)
docker compose build
# GPU-local: torch cu128 (+ на Шаге 5 EMBEDDING_DEVICE=cuda и раскомментировать deploy.resources)
docker compose build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
# Cloud: лёгкий образ без torch
docker compose build --build-arg EXTRAS=cloud-embeddings
```
Альтернативы для Windows (если не хотите `--build-arg`):
```powershell
# PowerShell:
$env:TORCH_INDEX_URL="https://download.pytorch.org/whl/cu128"; docker compose build
```
```cmd
:: CMD:
set "TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128" && docker compose build
```
Самое надёжное — задать `EXTRAS`/`TORCH_INDEX_URL` в `.env` (compose читает его сам для `${...}`),
затем обычный `docker compose build`.

- `EXTRAS` — какие зависимости вшить (см. Шаг 0/3): `local-embeddings` | `cloud-embeddings`.
- `TORCH_INDEX_URL` — источник torch (`…/whl/cpu` | `…/whl/cu128`); влияет только при `EXTRAS=local-embeddings`.
- Образ ставит и системный `git` (для ингеста git-источников), и extra `ingest` (pyyaml).

---

## Шаг 5. Запустить и проверить связность

```bash
# поднять Neo4j + app (app дождётся healthy Neo4j)
docker compose up -d --wait
docker compose ps                 # оба контейнера healthy
docker compose logs app --tail 20 # старт сервера, выбранное устройство эмбеддингов
```
Проверка БД из контейнера: `docker compose run --rm app onec-vecgraph health` (печатает edition/узлы).
Через MCP-клиент (Шаг 8): инструменты `ping`, `neo4j_health`, `whoami` (подтверждает разрешённый tenant).

---

## Шаг 6. Проиндексировать конфигурацию (каждый флаг)

Выгрузку 1С (формат Конфигуратора) монтируем внутрь — путь **runtime-том**, в образ не зашит.

```bash
# 6.1 граф метаданных (объекты/реквизиты/типы/подсистемы/права/формы/WRITES_TO/:Detail)
docker compose run --rm -v "/path/to/ERP_UH:/dump:ro" app \
  onec-vecgraph index /dump --tenant-id erp --reset
#   index <dir>      — каталог выгрузки (примонтирован как /dump:ro = только чтение).
#   --tenant-id erp  — арендатор/проект (изоляция). Отдельный на каждую конфигурацию/компанию.
#   --reset          — пересоздать граф этого tenant (первый прогон). Без флагов = неразрушающий MERGE.
#   --incremental    — (вместо --reset) обновить только изменённые объекты по configVersion.

# 6.2 граф вызовов BSL (рутины, CALLS, HANDLES, точки входа, менеджерные вызовы)
docker compose run --rm app onec-vecgraph callgraph --tenant-id erp
#   --incremental    — только изменённые модули (фолбэк на полный при правках общих модулей).

# 6.3 векторизация (чанки + эмбеддинги + индексы); ТЕМ ЖЕ провайдером/моделью, что и сервер
docker compose run --rm app onec-vecgraph vectorize --tenant-id erp --code
#   --code           — также векторизовать код модулей (cAST-чанкинг). Без него — только метаданные/формы.
#   --incremental    — переэмбеддить только объекты с изменившимся configVersion.
#   --no-reset       — не стирать существующие чанки (по умолчанию vectorize пересоздаёт).
```
Тяжесть: `vectorize --code` на ERP — десятки минут (GPU) и дольше (CPU/cloud). Для local разумно гонять на
GPU-образе, сервить — на CPU/cloud. Повторить 6.1–6.3 для других конфигураций (свой `--tenant-id`).

---

## Шаг 7. Доки и справка платформы (каждый флаг)

**Проектные доки (ИТС/git-артефакты) — в tenant проекта**, через манифест (`sources.example.yaml`):
```bash
docker compose run --rm -v "/path/sources.yaml:/m.yaml:ro" app \
  onec-vecgraph ingest /m.yaml --tenant-id erp [--only its|git_artifacts] [--reset] [--link-semantic]
#   ingest <manifest>     — YAML/JSON со списком источников.
#   --only <type>         — заингестить только источники этого типа.
#   --reset               — пересобрать корпус с нуля (иначе инкремент по version_hash).
#   --link-semantic       — дополнительно создать рёбра RELATES_TO к ближайшим объектам (дороже).
```

**Справка платформы (синтаксис-помощник, .hbk) — публичная, в ОБЩИЙ тенант, с проверкой пути:**
```bash
docker compose run --rm -v "C:/Program Files/1cv8/8.3.27.1989/bin:/pf-bin:ro" app \
  onec-vecgraph ingest-help --tenant-id __shared__ --bin /pf-bin --domain shcntx --domain shlang
#   ingest-help            — запуск векторизации справки (CLI-only; MCP read-only). ВАЛИДИРУЕТ путь:
#                            если не задан/не найден .hbk → ошибка и exit 1 (а не тихий 0).
#   --tenant-id __shared__ — общий публичный тенант (читается всеми проектами автоматически).
#   --bin /pf-bin          — каталог bin платформы; адаптер сам найдёт sh*_ru.hbk. Версия берётся из пути.
#   --domain shcntx/shlang — какие домены справки: shcntx (объекты/методы), shlang (язык), shquery (запросы).
#   --file <путь.hbk>      — (вместо --bin) явные файлы (повторяемо).
#   --platform-version X   — переопределить версию (иначе из пути).
#   --limit N              — ограничить число страниц (смоук/дев).
#   --reset                — пересобрать справку этой версии.
```
Контент справки/ИТС **проприетарный** → общий тенант и репозитории источников держать **приватными**.

---

## Шаг 8. Подключить MCP-клиента

**Режим bearer (рекомендуется; `AUTH_ENABLED=true`):**
```json
{ "mcpServers": { "onec-vecgraph": {
  "url": "https://<host>/mcp",                 // через TLS-прокси (Шаг 10)
  "headers": { "Authorization": "Bearer tok_a" } // токен из AUTH_TOKENS → определяет tenant
}}}
```
**Режим доверенного заголовка (`AUTH_ENABLED=false`, только за gateway):**
```json
{ "headers": { "X-Tenant-Id": "erp", "X-Config-Id": "base" } }
```
Tenant **только из заголовка/токена** (не из аргументов инструментов). Справка платформы доступна
автоматически — отдельных параметров tenant агенту не нужно.

---

## Шаг 9. Смоук-проверка (через MCP-клиента или CLI)

```bash
docker compose run --rm app onec-vecgraph metrics --tenant-id erp     # инвентарь: объекты/рутины/чанки
docker compose run --rm app onec-vecgraph search "проведение реализации" --tenant-id erp --mode hybrid
docker compose run --rm app onec-vecgraph docinfo "Массив.Найти" --tenant-id erp   # справка платформы (из __shared__)
```
Через MCP: `whoami` (tenant), `hybrid_search`, `docinfo`. **Пустой результат поиска/графа** обычно значит
«слой не построен для этого tenant» (не проиндексирован), а не «не найдено» — вернитесь к Шагам 6–7.

---

## Шаг 10. Прод-хардненинг (зачем каждый пункт)

- **TLS-прокси** (nginx/traefik) перед `app`; порт `app` не публиковать наружу напрямую (сейчас loopback).
- **`AUTH_ENABLED=true` + секретные `AUTH_TOKENS`** (docker/k8s secret, не в git) — иначе любой, кто
  достучался до порта, выберет tenant заголовком.
- **`NEO4J_PASSWORD`** не дефолтный; бэкап тома `./data/neo4j`.
- **Одна модель эмбеддингов** на всю БД (вкл. `__shared__`) — инвариант (см. правило в Шаге 2).
- **Реплики `app`** за балансировщиком при нагрузке (`stateless_http` — безопасно), общий Neo4j.
- **Обновления** — периодический `index|vectorize|callgraph --incremental`; справка — `ingest-help --reset` на новую сборку.

---

## Приложение. Полная таблица переменных

| Переменная | Дефолт | Назначение | Когда менять |
|---|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | адрес Neo4j | внешний/удалённый Neo4j (в compose уже `bolt://neo4j:7687`) |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j` / dev | доступ к БД | пароль — всегда в проде |
| `NEO4J_DATABASE` | `neo4j` | имя БД | db-per-tenant (Enterprise) |
| `EMBEDDING_PROVIDER` | `hashing` (в compose `local`) | бэкенд эмбеддингов | выбор профиля |
| `EMBEDDING_MODEL` | `Qwen3-Embedding-0.6B` | модель | качество (4B) / облако / bge-m3 |
| `EMBEDDING_DEVICE` | `cpu` | local: cpu/cuda/auto | GPU-профиль → cuda |
| `EMBEDDING_BATCH_SIZE` | `16` | батч эмбеддинга | масштаб → 32–64 |
| `EMBEDDING_MAX_SEQ_LENGTH` | `256` | токенов на чанк | длинные чанки + есть память |
| `EMBEDDING_DIM` | `256` | размерность hashing | только для hashing |
| `EMBEDDING_DIMENSIONS` | — | cloud: усечение размерности | OpenAI 3-*/Voyage 3.5 |
| `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`VOYAGE_API_KEY` | — | облачные доступы | provider=openai/voyage |
| `HF_TOKEN` | — | токен HuggingFace | снять rate-limit на скачивание модели |
| `RERANK_ENABLED`/`RERANK_MODEL` | `false` / bge-reranker | реранкер | нужна точность поверх RRF |
| `MCP_HOST`/`MCP_PORT`/`MCP_PATH` | `127.0.0.1`/`8000`/`/mcp` | эндпоинт MCP | в контейнере host=0.0.0.0 |
| `REQUIRE_TENANT` | `true` | требовать tenant по HTTP | оставить true в сети |
| `DEFAULT_TENANT_ID`/`DEFAULT_CONFIG_ID` | `default`/`base` | дефолт для stdio/CLI | под локальный проект |
| `SHARED_TENANT_ID` | `__shared__` | общий публичный тенант | при переименовании |
| `INCLUDE_SHARED_TENANT` | `true` | аддитивно читать общий тенант | отключить публичные корпуса |
| `AUTH_ENABLED` | `false` | bearer-аутентификация | true для сетевого доступа |
| `AUTH_TOKENS` | — | карта токен=tenant[:config] | при AUTH_ENABLED=true |
| `EXTRAS` (build) | `local-embeddings` | состав образа | cloud-embeddings для лёгкого образа |
| `TORCH_INDEX_URL` (build) | `…/whl/cpu` | источник torch | `…/whl/cu128` для GPU |
