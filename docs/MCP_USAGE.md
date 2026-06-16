# Как пользоваться MCP-сервером `onec-vecgraph` (для агентов/ролей)

Гайд для **потребителя** MCP (стороннего агента/роли). Как поднять и переиндексировать — см.
[../README.md](../README.md); полное состояние — [STATE.md](STATE.md).

`onec-vecgraph` — **read-only** база знаний по конфигурации 1С (из XML-выгрузки Конфигуратора) в Neo4j:
граф метаданных + граф зависимостей/вызовов BSL + векторный и полнотекстовый поиск. Инструменты
**не меняют** конфигурацию — только отвечают на вопросы о ней. Тот же overview сервер отдаёт клиенту
в поле `instructions` при `initialize`.

---

## 1. Подключение

### HTTP (основной режим)
```
onec-vecgraph serve --transport http     # по умолчанию http://127.0.0.1:8000/mcp
```
Транспорт — **Streamable HTTP**, `stateless_http` (состояние сессии не хранится; арендатор — на каждый запрос).

**Привязка арендатора — два режима** (определяется сервером):

**A. Bearer-токен (рекомендуется для сетевого доступа; `AUTH_ENABLED=true`).** Каждый вызов несёт
`Authorization: Bearer <token>`; tenant (и опц. config) берутся из карты токенов на сервере (`AUTH_TOKENS`),
а НЕ из заголовка `X-Tenant-Id` (его подделать нельзя — он игнорируется). `X-Config-Id` может уточнить
config, если токен его не закрепляет. Без валидного токена — `TenantResolutionError`.
```json
{
  "mcpServers": {
    "onec-vecgraph": {
      "url": "http://host:8000/mcp",
      "headers": { "Authorization": "Bearer tok_abc" }
    }
  }
}
```

**B. Доверенный заголовок (`AUTH_ENABLED=false`, legacy/внутренняя сеть).** Каждый вызов несёт
`X-Tenant-Id: <tenant>` (опц. `X-Config-Id`). Без `X-Tenant-Id` запрос отклоняется (`require_tenant`).
**Используйте только за аутентифицирующим gateway** — заголовок принимается без проверки.
```json
{
  "mcpServers": {
    "onec-vecgraph": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "X-Tenant-Id": "demo", "X-Config-Id": "base" }
    }
  }
}
```
Проверка привязки в обоих режимах: вызвать **`whoami`** → `{tenant_id, config_id}`.

### stdio (локальная отладка)
```
onec-vecgraph serve --transport stdio
```
Заголовков нет → берётся дефолтный tenant из `.env` (`default_tenant_id`/`default_config_id`).

---

## 2. Доступность данных по арендатору (ВАЖНО)

У арендатора есть данные только для тех слоёв, которые для него построены:

| Слой | Чем строится | Нужен для инструментов |
|---|---|---|
| Граф метаданных + `:Detail` | `index` | list_metadata, get_object, get_object_properties, get_dependencies, impact_analysis, find_type_usages |
| Векторы/чанки | `vectorize` (+`--code`) | semantic_search, hybrid_search |
| Граф вызовов BSL | `callgraph` | find_callers, find_callees, call_path, find_handlers, и `entry_points`/`calls_by_kind` в metrics |
| Doc-корпуса (ИТС / артефакты) | `ingest` | search с `source=['its'\|'artifact']`, find_related_docs, get_document |
| Справка платформы (`.hbk`) | `ingest-help` (CLI оператора) | search с `source=['platform_help']`, **`docinfo`**, get_document; читается из общего тенанта |

> Загрузка/векторизация корпусов — операция **оператора через CLI** (`ingest` / `ingest-help`); сам MCP
> **read-only** и индексацию не запускает. Если справка/доки не отвечают — их ещё не заингестили.

**Пустой результат поиска или графа вызовов обычно означает «слой не построен для этого tenant», а не
«ничего не найдено».** Сначала проверьте `metrics` (есть ли routines/chunks).

---

## 3. Идентификация объектов (fqn) и словари

**fqn = `<Вид>.<Имя>`**: `Catalog.Контрагенты`, `Document.РеализацияТоваров`, `Enum.СтатусыЗаказов`,
`CommonModule.ОбщегоНазначения`, `InformationRegister.Цены`, `AccumulationRegister.Продажи`,
`Subsystem.Продажи`, `Role.Администратор`. Вложенная подсистема: `Subsystem.Родитель.Subsystem.Дочерняя`.
**fqn рутины** = `<fqn-модуля>::<ИмяМетода>`, напр. `Document.РеализацияТоваров.Module.ObjectModule::ОбработкаПроведения`.
Большинство инструментов принимают и fqn, и просто имя объекта.

**Виды объектов (`kinds`)**: Catalog, Document, Enum, InformationRegister, AccumulationRegister,
AccountingRegister, CalculationRegister, ChartOfCharacteristicTypes, ChartOfAccounts, CommonModule,
Report, DataProcessor, Constant, Subsystem, Role, EventSubscription, BusinessProcess, Task,
ExchangePlan, DocumentJournal, DefinedType, CommonForm, … (полный список — через `metrics`).

**Корпуса (`source`)**: `config` (сама конфигурация 1С), `its` (документация ИТС), `artifact`
(проектные документы из git), `platform_help`/`bsp_help` (**общедоступная** справка платформы/БСП).
Каждый хит несёт поле `corpus`. Публичные корпуса (`platform_help`/`bsp_help`) живут в общем тенанте и
читаются **автоматически** в дополнение к вашему — никаких доп. аргументов/заголовков не нужно (агент
по-прежнему шлёт один `X-Tenant-Id`; общий тенант добавляет сервер).

**Версия платформы (`platform_version`) — контракт обращения.** Справка платформы/БСП **версионная**, и
несколько версий сосуществуют в общем тенанте (`fqn = platform_help:<версия>|<Имя>` уникален per-version).
Правила:
- **`platform_version` — это фильтр релевантности, не граница изоляции** (изоляция — только тенант, его задаёт сервер). Версию передаёт клиент/оркестратор как аргумент запроса.
- **Передавать `platform_version` только вместе с `source=['platform_help']`** (или `bsp_help`). Фильтр идёт по версии **документа-владельца**; у объектов конфигурации версии нет, поэтому при заданной версии они **выпадут** из выдачи. Для запросов к конфигурации/коду/графу `platform_version` **не указывать**.
- **Без `platform_version`**: поиск охватывает все загруженные версии; `docinfo` при нескольких совпадениях вернёт `candidates` (по версии у каждого) для выбора, при одном — сразу статью.
- **Адресная выборка** конкретной версии: `get_document("platform_help:<версия>|<Имя>")` — версия в `fqn`.
- **Мультиверсионный оркестратор**: держите маппинг `проект → platform_version` у себя и инжектьте версию в help-вызовы (`docinfo`, поиск с `source=['platform_help']`); если точной версии нет — фолбэк на ближайшую (наибольшую ≤ запрошенной в той же `major.minor`).

**Классификация документов (`doc_topic` / `corpus_version` / `help_kind`) — фасеты владельца, ортогональные `source`:**
- **`doc_topic`** — о чём документ: `platform` (справка/методика платформы) · `config` (документация по конфигурации) · `task` (проектная/задачная). Разделяет, например, платформенную и конфигурационную части ИТС.
- **`corpus_version`** — типизированная версия `<схема>:<значение>`: `config:ERP_2.5.18` (релиз конфигурации) · `task:JIRA-1234` / `git:<тег>`. Для справки платформы версия — это `platform_version` (в `corpus_version` не дублируется).
- **`help_kind`** — вид страницы синтаксис-помощника: `context` / `language` / `query`.
- Все три — предикаты по **узлу-владельцу**: как и `platform_version`, передавайте их **только вместе с соответствующим `source`** (иначе корпуса без этого поля, напр. конфигурация, выпадут). Это фильтры релевантности; изоляцию по-прежнему обеспечивает только тенант.

**Виды чанков (`chunk_kinds`)**: `object`, `attribute`, `tabular_attribute`, `enum_value`,
`predefined`, `form`, `code`, `subsystem`, `role`, `its`, `artifact`.

**Категории точек входа (`entry_point`)**: `проведение`, `запись`, `удаление`, `заполнение`,
`проверка_заполнения`, `нумерация`, `выбор`, `событие_формы`.

**Граф вызовов `CALLS`**: `kind` ∈ `local` / `common_module` / `manager`; `confidence` ∈ `high` / `medium`.
Неразрешённые вызовы (платформенные, через переменную-объект) в граф НЕ пишутся.

---

## 4. Карта инструментов по потребности (19)

### Здоровье / контекст
- **`ping`** — живость сервера (имя/версия).
- **`neo4j_health`** — связность с Neo4j (edition, число узлов).
- **`whoami`** — какой tenant/config разрезолвился для запроса (проверка заголовков).

### Поиск (нужна векторизация)
- **`hybrid_search(query, top_k=10, source?, kinds?, chunk_kinds?, subsystem?, platform_version?, doc_topic?, corpus_version?, help_kind?, expand?)`** —
  мульти-вектор + полнотекст + RRF. **Дефолт для поиска.** Идентификаторы расщепляются на под-слова (`Продажи`↔`ПродажиТоваров`).
- **`semantic_search(...)`** — только векторный (та же сигнатура).
- Фильтры: `source` (корпус: `config`/`its`/`artifact`/`platform_help`/`bsp_help`), `kinds` (виды объектов),
  `chunk_kinds` (виды чанков), `subsystem` (имя/fqn — объекты подсистемы и её потомков); фасеты владельца
  `platform_version` / `doc_topic` / `corpus_version` / `help_kind` (передавать вместе с соответствующим `source`).
  `expand=True` → к хиту добавляется `context`.
- Хиты по коду несут `routine_fqn`/`routine` (адрес рутины — можно сразу подать в `find_callers`/`find_callees`);
  каждый хит несёт `corpus`.
- Результат: `{query, mode, results:[{fqn, kind, synonym, via, corpus, matched, rrf_score, routine_fqn?, routine?, context?}]}`.

### Структура объекта (граф метаданных)
- **`list_metadata(kind?, name_contains?, limit=200)`** — список объектов (точный фильтр, не семантика).
- **`get_object(query, detail=False)`** — карточка: реквизиты+типы, ТЧ, значения перечислений,
  предопределённые, формы, модули, владельцы, подсистемы. `detail=True` добавляет полный сырой набор свойств.
- **`get_object_properties(query)`** — полный сырой `<Properties>` (Hierarchical, CodeLength, Posting,
  Periodicity, FullTextSearch, DataLockControlMode, ChoiceMode, …). Это «глубокая справка»; **не векторизуется**.

### Зависимости / влияние
- **`get_dependencies(query, direction='both')`** — связи объекта (`out`/`in`/`both`): ссылки по типам +
  CONTAINS/OWNED_BY/SUBSCRIBES/HAS_RIGHT_ON/**WRITES_TO**/…
- **`impact_analysis(query)`** — кто пострадает при изменении (входящие зависимости).
- **`find_type_usages(query)`** — где объект используется как тип реквизита/измерения/ресурса.

### Документация по объекту (нужен ingest корпусов ИТС/артефактов)
- **`find_related_docs(query)`** — доки (ИТС/артефакты), связанные с объектом через `MENTIONS`
  (явные/сканированные fqn) или `RELATES_TO` (семантика, с `confidence`). «Какие стандарты/доки покрывают объект».
- **`get_document(fqn)`** — документ по fqn-владельца (`its:<id>` / `platform_help:<ver>|<Имя>`, напр. из `fqn` хита):
  метаданные, полный текст (чанки склеены), связанные объекты.
- **`docinfo(name, platform_version?)`** — синтаксис-помощник платформы: точный лукап справки по
  каноническому имени (RU / English / `Объект.Метод`, напр. `ТаблицаЗначений`, `Массив.Найти`, `QuerySchema`).
  Одно совпадение → полная статья; несколько → список `candidates`. Версия опц. (иначе последняя проиндексированная).
- Поиск по докам: `hybrid_search(query, source=["its"|"artifact"|"platform_help"], platform_version="8.3.27.1989")`;
  `expand=True` добавит `context.links`. Публичная справка (platform_help/bsp_help) читается из общего тенанта автоматически.

### Поведение / код (нужен граф вызовов)
- **`find_handlers(query)`** — точки входа объекта: обработчики событий форм (event→рутина) + стандартные
  события модулей (проведение/запись/проверка_заполнения/нумерация/…). Ответ на «что срабатывает при проведении/записи».
- **`find_callers(query)`** / **`find_callees(query)`** — кто вызывает рутину / что вызывает рутина.
- **`call_path(from_routine, to_routine)`** — кратчайший путь вызовов между двумя рутинами.

### Обзор
- **`metrics(subsystem?)`** — объекты по видам, объём кода, рёбра графа вызовов по kind/confidence,
  точки входа, хотспоты fan-in/fan-out. Опц. scope по подсистеме.

---

## 5. Слои глубины данных (дёшево → детально)

1. **Обнаружение** — `hybrid_search` / `list_metadata` / `metrics`.
2. **Семантическая структура** — `get_object` (что есть у объекта, в смысловом виде).
3. **Исчерпывающая справка** — `get_object_properties` / `get_object(detail=True)` (каждое сырое свойство).

Слой 3 **намеренно не входит в поиск** (чтобы не разбавлять смысловой индекс) — забирайте его по fqn, когда
нужны точные конфигурационные факты.

---

## 6. Типовые сценарии (сквозные)

**«Найти, где реализован расчёт себестоимости»**
1. `hybrid_search("расчёт себестоимости", chunk_kinds=["code"], expand=True)` → берём `routine_fqn` верхнего хита.
2. `find_callers(routine_fqn)` / `find_callees(routine_fqn)` — кто вызывает / что вызывает.

**«Что произойдёт при проведении документа РеализацияТоваров»**
1. `find_handlers("Document.РеализацияТоваров")` → рутины с `entry_point=проведение`.
2. `get_dependencies("Document.РеализацияТоваров", "out")` → движения (`WRITES_TO`) по регистрам.

**«Полная карточка + точные свойства справочника»**
1. `get_object("Catalog.Контрагенты")` — структура.
2. `get_object_properties("Catalog.Контрагенты")` — Hierarchical, CodeLength, и т.п.

**«Что сломается, если изменить Enum.СтатусыЗаказов»**
1. `impact_analysis("Enum.СтатусыЗаказов")` или `find_type_usages("Enum.СтатусыЗаказов")`.

**«Обзор подсистемы Продажи»**
1. `metrics(subsystem="Продажи")` — состав/объём/хотспоты.
2. `hybrid_search("…", subsystem="Продажи")` — поиск в пределах подсистемы.

---

## 7. Замечания / ограничения

- Все инструменты **read-only** и **строго ограничены** арендатором из заголовка (не из аргументов).
- Пустой результат поиска/графа вызовов ⇒ скорее «слой не построен», проверьте `metrics`.
- Структурные свойства в `:Detail` (StandardAttributes/InputByString/Characteristics) хранятся как
  raw-XML (обрезка ~2000 симв.) — скаляры (Hierarchical/CodeLength/…) чистые.
- Менеджерные вызовы (`Справочники.X.Метод`) — `confidence=medium`; платформенные/через-переменную вызовы
  в граф не попадают (полнота графа вызовов неполная by design).
- HANDLES доступен и как ребро графа, и через `find_handlers`.
