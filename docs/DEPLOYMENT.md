# Деплой `onec-vecgraph` и варианты настройки образа

Как развернуть сервер в Docker, какие бывают варианты образа и как их конфигурировать.
Пошаговый чеклист развёртывания — [DEPLOY_RUNBOOK.md](DEPLOY_RUNBOOK.md); потребительский гайд по
вызову инструментов — [MCP_USAGE.md](MCP_USAGE.md); состояние — [STATE.md](STATE.md).

---

## 1. Архитектура развёртывания

Два режима работы одного образа:

- **Online (сервис):** контейнер `app` поднимает MCP по Streamable HTTP. При поиске эмбеддится только
  **текст запроса** (одна короткая строка на вызов) → дёшево на CPU или через облачный API.
- **Offline (конвейер):** тот же образ/CLI выполняет тяжёлые `index` / `vectorize` / `callgraph`.
  Это разовые/периодические задачи; для `vectorize --code` на больших конфигурациях разумен GPU-хост.

```
              ┌─────────────┐         ┌──────────────────────┐
  агенты ───► │  app (MCP)  │ ──Bolt► │  Neo4j (граф+векторы) │
  (HTTP)      └─────────────┘         └──────────────────────┘
                                              ▲
                       offline: index / vectorize / callgraph (CLI, тот же образ)
```

> **Главное правило:** провайдер и модель эмбеддингов при `vectorize` и при онлайн-запросах
> **должны совпадать** — размерность векторного индекса фиксируется при векторизации.

---

## 2. Варианты образа (build-args)

| Вариант | Команда сборки | torch | Когда |
|---|---|---|---|
| **CPU local** (по умолч.) | `docker build -t onec-vecgraph .` | CPU | Локальная модель, без GPU. Дёшево для онлайн-запросов. |
| **GPU local** | `docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 -t onec-vecgraph:gpu .` | cu128 | Векторизация/запросы на GPU (RTX 50xx). Нужен NVIDIA Container Toolkit + `--gpus all`. |
| **Cloud** (без torch) | `docker build --build-arg EXTRAS=cloud-embeddings -t onec-vecgraph:cloud .` | — | Эмбеддинги через OpenAI/Voyage. Лёгкий образ, без ML-зависимостей. |

Аргументы: `EXTRAS` ∈ `local-embeddings` | `cloud-embeddings`; `TORCH_INDEX_URL` (только для local).

---

## 3. Переменные окружения

| Группа | Переменная | Назначение |
|---|---|---|
| Neo4j | `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | подключение (в compose `bolt://neo4j:7687`) |
| MCP | `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | в контейнере `0.0.0.0` / `8000` / `/mcp` |
| Провайдер | `EMBEDDING_PROVIDER` | `local` \| `openai` \| `voyage` \| `hashing` |
| Local | `EMBEDDING_MODEL`, `EMBEDDING_DEVICE` (`cpu`\|`cuda`\|`auto`), `HF_HOME` | модель ST, устройство, кеш модели |
| Cloud | `OPENAI_API_KEY`, `OPENAI_BASE_URL` (для совместимых шлюзов), `VOYAGE_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` (опц. ≤4096) | ключи и модель облака |
| Аренда | `REQUIRE_TENANT`, `DEFAULT_TENANT_ID`, `DEFAULT_CONFIG_ID` | изоляция/дефолты |
| Auth | `AUTH_ENABLED`, `AUTH_TOKENS` | bearer-токен → tenant (см. §7) |

Полный шаблон — [../.env.example](../.env.example).

**Примеры провайдеров:**
- Local CPU: `EMBEDDING_PROVIDER=local EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B EMBEDDING_DEVICE=cpu`
- Local GPU качество: `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B EMBEDDING_DEVICE=cuda`
- OpenAI: `EMBEDDING_PROVIDER=openai EMBEDDING_MODEL=text-embedding-3-large OPENAI_API_KEY=sk-…`
- OpenAI-совместимый шлюз: `… OPENAI_BASE_URL=https://gateway.example/v1`
- Voyage: `EMBEDDING_PROVIDER=voyage EMBEDDING_MODEL=voyage-3 VOYAGE_API_KEY=…`

---

## 4. Быстрый старт (docker compose)

`docker-compose.yml` содержит `neo4j` + `app`. Переменные берутся из окружения/`.env`.

```bash
# CPU local (по умолчанию)
docker compose up -d --build

# Cloud (лёгкий образ, эмбеддинги OpenAI)
EXTRAS=cloud-embeddings EMBEDDING_PROVIDER=openai \
  EMBEDDING_MODEL=text-embedding-3-large OPENAI_API_KEY=sk-… \
  docker compose up -d --build

# GPU local: build с cu128 + раскомментировать deploy.resources в compose, EMBEDDING_DEVICE=cuda
TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 EMBEDDING_DEVICE=cuda \
  docker compose up -d --build
```
MCP доступен на `http://127.0.0.1:8000/mcp`. Порт намеренно слушает только loopback —
наружу публиковать через reverse-proxy с TLS (§8).

---

## 5. Офлайн-конвейер индексации (в контейнере)

Образ содержит CLI `onec-vecgraph`. Выгрузку 1С монтируем внутрь и запускаем разово:

```bash
# построить граф метаданных (+WRITES_TO, +:Detail)
docker compose run --rm -v /path/to/ERP_UH:/dump:ro app \
  onec-vecgraph index /dump --tenant-id acme --reset

# граф вызовов BSL (рутины, CALLS, HANDLES, entry_points)
docker compose run --rm app onec-vecgraph callgraph --tenant-id acme

# векторизация (+ код); тем же провайдером/моделью, что и онлайн-сервис!
docker compose run --rm app onec-vecgraph vectorize --tenant-id acme --code
```
- Путь к выгрузке — **runtime-том** (`-v …:/dump`), он НЕ зашит в образ; образ конфигурация-агностичен.
- На Windows-хосте путь монтирования: `-v C:\1C\xml\ERP_UH:/dump:ro`.
- `vectorize --code` тяжёлый (часы на больших конфигурациях) — для local-эмбеддингов запускайте на
  **GPU-образе/хосте**; для cloud — ограничение по rate limit/стоимости API.
- Инкремент: `index|vectorize|callgraph --incremental` (только изменённые объекты).

### 5.1. Несколько конфигураций / арендаторов

Один образ и один запущенный сервер обслуживают **любое число конфигураций** — данные хранятся в Neo4j,
разделённые по `(tenant_id, config_id)`; маршрутизация на каждый запрос (заголовок/токен, см. §7).
В образ ничего про конкретную конфигурацию не попадает.

- **Разные продукты/компании** (ERP, УТ, …) → разные `--tenant-id` (полная изоляция).
- **База + расширения (.cfe) одной конфигурации** → один tenant, разные `config_id` (`base`/`ext:<Имя>`) —
  парсер проставляет это автоматически при индексации каталога выгрузки.

Удобно примонтировать **родительский каталог** с выгрузками и проиндексировать по очереди:
```bash
# на хосте: C:\1C\xml\ERP_UH и C:\1C\xml\UT
docker compose run --rm -v C:\1C\xml:/dumps:ro app onec-vecgraph index /dumps/ERP_UH --tenant-id erp --reset
docker compose run --rm -v C:\1C\xml:/dumps:ro app onec-vecgraph index /dumps/UT     --tenant-id ut  --reset
docker compose run --rm app onec-vecgraph callgraph --tenant-id erp
docker compose run --rm app onec-vecgraph callgraph --tenant-id ut
docker compose run --rm app onec-vecgraph vectorize --tenant-id erp --code
docker compose run --rm app onec-vecgraph vectorize --tenant-id ut  --code
```
Тот же сервер затем отдаёт обе: `X-Tenant-Id: erp` → ERP, `X-Tenant-Id: ut` → УТ (или bearer-токены
`AUTH_TOKENS="tok_erp=erp,tok_ut=ut"`). Состояние индексации по каждому tenant видно через `metrics`.

---

## 6. Доставка модели эмбеддингов (local)

Первый запрос/векторизация на local-провайдере качает модель (~1.2 ГБ для 0.6B) в `HF_HOME=/models`.
Том `./data/hf-cache:/models` (в compose) делает кеш постоянным между перезапусками.
Альтернативы: «прогреть» одним запросом после старта, либо запечь модель в образ
(`huggingface-cli download <model>` на этапе сборки) для иммутабельных образов. **Cloud-вариант
модель не качает.**

---

## 7. Аутентификация

Два режима (деталь — [MCP_USAGE.md §1](MCP_USAGE.md)):

- **Bearer-токен (рекомендуется для сетевого доступа):**
  ```bash
  AUTH_ENABLED=true AUTH_TOKENS="tok_acme=acme,tok_glx=globex:ext_crm" docker compose up -d
  ```
  Клиент шлёт `Authorization: Bearer tok_acme`; tenant берётся из карты на сервере, `X-Tenant-Id`
  игнорируется (подделать нельзя). Храните `AUTH_TOKENS` в секрете (docker/k8s secret, не в git).
- **Доверенный `X-Tenant-Id` (`AUTH_ENABLED=false`):** только за аутентифицирующим gateway,
  который сам проставляет заголовок. Прямой сетевой доступ в этом режиме небезопасен.

---

## 8. Прод и масштабирование

- **Stateless:** сервер `stateless_http` (арендатор — на каждый запрос) → можно держать **несколько
  реплик** `app` за балансировщиком; общий Neo4j.
- **TLS/доступ:** ставьте reverse-proxy (nginx/traefik) с TLS перед `app`; порт `app` не публиковать
  наружу напрямую.
- **Neo4j:** в проде — отдельный управляемый/выделенный Neo4j (память: heap 8G/pagecache 4G — см.
  compose); бэкапы тома `./data/neo4j`. Путь сильной изоляции арендаторов — БД-на-арендатора
  (Enterprise/Aura), слой хранилища это допускает.
- **Ресурсы:** local-CPU образу хватает скромного CPU/RAM для онлайн-запросов; векторизацию выносите
  на GPU-хост офлайн. Cloud-образ — минимальные ресурсы + сетевой доступ к API.
- **Health:** в образе `HEALTHCHECK` = `onec-vecgraph health` (проверяет Neo4j). MCP-инструменты
  `ping`/`neo4j_health` — для проверки из клиента.
- **Логи/кириллица:** в контейнере (Linux/UTF-8) проблем cp1251 нет; `PYTHONUNBUFFERED=1` уже задан.

---

## 9. Чеклист перед публичным выкатом

- [ ] `AUTH_ENABLED=true` + секретные `AUTH_TOKENS` (или строго за аутентифицирующим gateway).
- [ ] TLS-прокси перед `app`; прямой порт не опубликован наружу.
- [ ] Один и тот же `EMBEDDING_PROVIDER`/`EMBEDDING_MODEL` при `vectorize` и в сервисе.
- [ ] Для local — постоянный том `HF_HOME` (или модель запечена); для cloud — заданы ключи.
- [ ] Neo4j: пароль из секрета (не дефолтный), бэкап тома, достаточная память.
- [ ] Данные арендаторов проиндексированы (граф/векторы/callgraph) — иначе инструменты вернут пусто.
- [ ] Реплики `app` за балансировщиком при нагрузке (stateless — безопасно).
