# Руководство по векторизации корпусов знаний (agent-runnable)

Пошаговое руководство **как векторизовать тот или иной объект знаний** — пригодное для запуска
человеком-оператором ИЛИ ИИ-агентом (Claude / любая другая нейросеть). Описывает три корпуса:

| Корпус | Что это | Куда грузить | `source` | Инструмент чтения |
|---|---|---|---|---|
| **A. ИТС по конфигурации** | документация ИТС, привязанная к объектам конкретной конфигурации | **тенант проекта** | `its` | `its_find_related_docs` / `its_get_document` |
| **B. Стандарты разработки** | «Система стандартов и методик» 1С (v8std) — непроектные правила | **`__shared__`** | `its` | `dev_standards_search` / `dev_standards_get` |
| **C. Синтаксис-помощник платформы** | справка платформы из `.hbk`, версионная | **`__shared__`** | `platform_help` | `platform_docinfo` / `platform_get_document` |

> Операторская «единственная правда» по всем командам записи (index/callgraph/vectorize, reset-семантика) —
> [OPERATOR_PLAYBOOK.md](OPERATOR_PLAYBOOK.md). Здесь — целевой рецепт именно под векторизацию трёх
> корпусов знаний и типичные ошибки. Контракт ИТС-парсера — [ITS_PARSER_REQUIREMENTS.md](ITS_PARSER_REQUIREMENTS.md).

---

## 0. Инварианты (нарушение → тихая порча данных)

1. **Одна модель/размерность эмбеддингов на всю БД.** При `ingest`/`vectorize` `EMBEDDING_PROVIDER`+`EMBEDDING_MODEL`
   ДОЛЖНЫ совпадать с тем, что в `.env` работающего read-сервера. Векторный индекс один на БД — смешение
   моделей ломает поиск. Самый надёжный способ гарантировать совпадение — **запускать ингест внутри того же
   контейнера**, что и сервер (см. §1, способ 2).
2. **MCP read-only.** Векторизацию запускает ТОЛЬКО CLI (`onec-vecgraph ingest` / `vectorize` / `ingest-help`).
   В MCP инструмента записи нет by design.
3. **Размещение по тенанту = граница изоляции.** Проектную ИТС-документацию грузить в **тенант проекта**
   (чтобы рёбра `MENTIONS`/`RELATES_TO` линковались с объектами этой конфигурации). Непроектное (стандарты,
   справка платформы) — в **`__shared__`** (читается всеми аддитивно, без дублирования по проектам).
4. **Чанк рождается из `(title, text)`.** Если адаптер отдал юнит с пустым `text` — чанков НЕ будет
   (владелец-документ может остаться без `:Chunk` → поиск по нему пуст). См. §4 «Типичные ошибки».

---

## 1. Где запускать ингест

**Способ 1 — с хоста (`uv run`), основной checkout.** Рабочие тома `./data` и исходники живут в основном
каталоге (`D:\Claude\MCP for embedding`), не в worktree. Модель берётся из хостового `.env` — **проверьте,
что она совпадает с серверной**.
```bash
uv run onec-vecgraph health                       # связность Neo4j
uv run onec-vecgraph ingest <manifest> --tenant-id <t> [--reset]
```

**Способ 2 — внутри работающего контейнера (рекомендуется для деплоя).** Гарантирует ту же модель/размерность,
GPU и ту же БД, что у сервера. Исходные данные в контейнер не примонтированы (кроме `/models`) — кладём через
`docker cp`, затем запускаем `onec-vecgraph` внутри:
```bash
# 1) скопировать исходник и манифест в контейнер (Git Bash: export MSYS_NO_PATHCONV=1 чтобы /tmp не конвертился)
docker cp "<host>/its-out/v8std"  onec_vecgraph_app:/tmp/v8std
docker cp "<host>/manifest.yaml"  onec_vecgraph_app:/tmp/manifest.yaml   # path: внутри указывает на /tmp/v8std
# 2) запустить ДЕТАЧЕМ с логом (переживает обрыв docker exec-стрима):
docker exec -d onec_vecgraph_app sh -c \
  'onec-vecgraph ingest /tmp/manifest.yaml --tenant-id __shared__ --reset > /tmp/ingest.log 2>&1; echo "EXIT_$?" >> /tmp/ingest.log'
# 3) опрашивать лог до строки EXIT_0:
docker exec onec_vecgraph_app sh -c 'tail -5 /tmp/ingest.log'
```
> Файлы, скопированные `docker cp`, принадлежат root → удалять их потом нужно `docker exec -u root … rm -rf`.
> Изменения внутри контейнера переживают `restart`, но **теряются при пересоздании из образа** — это операция
> над данными в Neo4j (она durable), сами `/tmp`-файлы временны.

---

## A. ИТС-документация по КОНФИГУРАЦИИ

Документы ИТS, относящиеся к конкретной конфигурации (напр. УТ 11.5, ERP). Грузятся в **тенант проекта**, чтобы
линковаться с его объектами.

**Манифест** (`manifests/its-<config>.yaml`):
```yaml
tenant: grand-dev-ut          # тенант проекта (или задать --tenant-id)
sources:
  - type: its
    path: "D:/Claude/onec-its-parser/its-out/ut115doc"   # каталог JSON-юнитов от ИТС-парсера
    globs: ["**/*.json"]
    doc_topic: config          # фасет: документация по конфигурации
    corpus_version: "config:UT_11.5"   # пин релиза конфигурации (owner-фасет для фильтра поиска)
```

**Запуск:**
```bash
uv run onec-vecgraph ingest manifests/its-ut.yaml --tenant-id grand-dev-ut                  # инкремент по version_hash
uv run onec-vecgraph ingest manifests/its-ut.yaml --tenant-id grand-dev-ut --link-semantic  # + RELATES_TO к ближайшим объектам
uv run onec-vecgraph ingest manifests/its-ut.yaml --tenant-id grand-dev-ut --reset          # пересобрать корпус ИТС в этом тенанте
```
- `--link-semantic` строит рёбра `RELATES_TO` (семантическая близость документа к объектам конфигурации,
  с `confidence`). Без него линкуются только явные `MENTIONS` (fqn, упомянутые/просканированные).
- `--only its` — если в манифесте несколько типов источников, ингестить только ИТС.

**Проверка:**
```bash
# через CLI:
uv run onec-vecgraph search "проведение реализации" --tenant-id grand-dev-ut --mode hybrid
# через MCP (источник its): its_find_related_docs(<fqn объекта>) → its_get_document(<fqn документа>)
```

---

## B. Стандарты разработки 1С (v8std)

Непроектные правила «как писать по стандартам 1С». Грузятся в **`__shared__`**, читаются всеми тенантами.
Канонический манифест — [`manifests/its-v8std.yaml`](../manifests/its-v8std.yaml).

**Запуск с хоста:**
```bash
uv run onec-vecgraph ingest manifests/its-v8std.yaml --tenant-id __shared__ --reset
```

**Запуск в контейнере** (то, что делалось при восстановлении корпуса; гарантирует серверную модель):
```bash
export MSYS_NO_PATHCONV=1
docker cp "D:/Claude/onec-its-parser/its-out/v8std" onec_vecgraph_app:/tmp/v8std
docker cp "<...>/its-v8std.container.yaml"          onec_vecgraph_app:/tmp/its-v8std.yaml   # path: /tmp/v8std
docker exec -d onec_vecgraph_app sh -c \
  'onec-vecgraph ingest /tmp/its-v8std.yaml --tenant-id __shared__ --reset > /tmp/ingest.log 2>&1; echo "EXIT_$?" >> /tmp/ingest.log'
# опрашивать /tmp/ingest.log до EXIT_0
```
- `--reset` для этого манифеста удаляет **только `source='its'`** (`delete_source('its')`) в `__shared__` —
  справка платформы (`source='platform_help'`) НЕ затрагивается. Нужен потому, что инкрементальный ингест
  пропускает владельцев с тем же `version_hash` (см. §4).
- Стандарты уже в контракт-формате (`id=v8std_<n>`, `doc_topic=platform`, `corpus_version=platform:v8std`).

**Проверка** (через MCP, любой тенант — стандарты в `__shared__`, читаются аддитивно):
```
dev_standards_search("именование переменных")   → ранжированные хиты its:v8std_<n>
dev_standards_get("396")                          → полный текст одного стандарта
```
Ожидаемо: `dev_standards_search` возвращает непустой список, `dev_standards_get` — непустой `text`.

---

## C. Синтаксис-помощник платформы (.hbk) — по ВЕРСИИ

Справка платформы из контейнеров `.hbk` (`shcntx`/`shlang`/`shquery`). Грузится в **`__shared__`**,
**версионно**: `platform_version` берётся из пути `bin` и пишется на владельца-документ; квалифицированный
fqn — `platform_help:<версия>|<Имя>`.

**Запуск** (путь валидируется ДО старта — нет файла → понятная ошибка и `exit 1`, не «тихий 0»):
```bash
# из каталога bin платформы (автодискавери sh*_ru.hbk):
uv run onec-vecgraph ingest-help --tenant-id __shared__ \
  --bin "C:\Program Files\1cv8\8.3.27.1989\bin" --domain shcntx --domain shlang
# или явными файлами + явной версией:
uv run onec-vecgraph ingest-help --tenant-id __shared__ \
  --file "<...>\shcntx_ru.hbk" --platform-version 8.3.27.1989
```
- `--domain`: `shcntx` (контекст/объекты), `shlang` (язык), `shquery` (запросы). Дефолт — `shcntx`+`shlang`.
- `--limit N` — смоук-загрузка; `--reset` — пересобрать **эту версию** справки.
- **Мультиверсионность:** запускайте `ingest-help` для каждой нужной сборки (`8.3.27.1989`, `8.3.27.2130`, …) —
  они сосуществуют. Потребитель выбирает версию аргументом `platform_version` в поиске / `platform_docinfo`.
  Без версии — поиск охватывает все загруженные, `platform_docinfo` вернёт `candidates` по версиям.
- В контейнере нужен доступ к `.hbk`: смонтировать каталог bin (`-v "C:/.../bin:/pf-bin:ro"`) или `docker cp`.

**Проверка:**
```
platform_docinfo("ТаблицаЗначений" [, platform_version="8.3.27.2130"])  → полная статья / candidates
platform_get_document("platform_help:8.3.27.2130|ТаблицаЗначений")       → полный текст по fqn
```

---

## 2. Чек-лист агента (decision tree)

1. **Определи корпус и тенант.** Привязка к объектам конфигурации? → §A, тенант проекта. Стандарты? → §B, `__shared__`.
   Справка платформы? → §C, `__shared__`.
2. **Сверь модель.** Запусти в контейнере (§1, способ 2) ИЛИ убедись, что хостовый `EMBEDDING_MODEL` == серверный.
3. **Подготовь источник.** ИТС/стандарты — каталог JSON от парсера (контракт ИТС). Справка — каталог `bin`/файлы `.hbk`.
4. **Запусти ингест** с нужными флагами (первичная загрузка/восстановление → `--reset`; догрузка изменений → без него).
5. **Проверь результат** (§3): счётчики чанков с эмбеддингами > 0 И инструмент чтения возвращает данные.

---

## 3. Верификация (Cypher + MCP)

**Счётчики в Neo4j** (внутри контейнера; `$t='__shared__'` для B/C, тенант проекта для A; `$src` ∈ its|platform_help):
```cypher
MATCH (c:Chunk {tenant_id:$t, source:$src}) RETURN count(c) AS chunks;
MATCH (c:Chunk {tenant_id:$t, source:$src}) WHERE c.embedding IS NOT NULL RETURN count(c) AS with_emb;
MATCH (d:Document {tenant_id:$t}) WHERE d.source=$src RETURN count(d) AS owners;
```
Здоровый корпус: `owners > 0`, `chunks > 0`, `with_emb == chunks`.

**Через MCP** (предпочтительно — проверяет весь стек, включая auth/тенант): вызвать инструмент чтения корпуса
(см. таблицу вверху) и убедиться, что результат непустой и релевантный.

---

## 4. Типичные ошибки

| Симптом | Причина | Что делать |
|---|---|---|
| **`*_search` пуст, но `*_get` отдаёт метаданные без текста** | владельцы-документы есть, но **`:Chunk` нет** (корпус залит как метаданные без чанк/векторизации) | переингест с `--reset` (scoped по `source`); см. §B |
| **Инкремент «ничего не меняет», чанки не появились** | у владельцев тот же `version_hash` → инкремент считает их актуальными и пропускает | `--reset` ИЛИ удалить владельцев (`delete_docs` по fqn), затем ингест |
| **Юнит не попал в корпус совсем** | у записи источника пустой `text` — адаптер ИТС такие **пропускает** (нет владельца, нет чанка) | починить парсер/исходник: непустой `text` |
| **Поиск стал «мусорным» после смены модели** | в БД смешаны разные модели/размерности (единый вектор-индекс) | полный реиндекс всех тенантов одной моделью |
| **`platform_version` выкидывает результаты конфигурации** | версия фильтрует по полю владельца, у объектов конфигурации его нет | `platform_version` использовать ТОЛЬКО с `source=['platform_help']` |

---

## Глубже
- Все команды записи, reset/incremental-семантика, overlay — [OPERATOR_PLAYBOOK.md](OPERATOR_PLAYBOOK.md).
- Как потребители читают (read-only API) — [MCP_USAGE.md](MCP_USAGE.md).
- Деплой/настройки — [DEPLOYMENT.md](DEPLOYMENT.md), [DEPLOY_DETAILED.md](DEPLOY_DETAILED.md), `.env.example`.
- Снимок состояния и инварианты — [STATE.md](STATE.md).
