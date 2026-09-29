# Старт сессии — окружение и предполёт (anti-error)

**Цель:** убрать типовые ошибки в самом начале сессии (до `index`/`callgraph`/`vectorize`/`ingest`).
Прочитай это первым, если собираешься запускать CLI. Сами операции — в
[OPERATOR_PLAYBOOK.md](OPERATOR_PLAYBOOK.md). Инварианты/состояние — в [STATE.md](STATE.md).

Среда проекта: **Windows + PowerShell**, `uv`, Neo4j в Docker. Пути к инструментам
и кешам принадлежат машине и в Git не фиксируются.

---

## 1. Однострочный префикс (главное средство от ошибок)

Окружение свежей сессии PowerShell может быть не настроено: сначала проверь `uv`
в PATH и кодировку. Внутри инструментов агента переменные окружения **не сохраняются
между вызовами** — поэтому добавляй этот нейтральный префикс в команды `uv …`:

```powershell
Get-Command uv -ErrorAction Stop | Out-Null; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; $env:PYTHONUTF8='1'
```

Что он проверяет/чинит: наличие `uv` · кириллица в выводе (UTF-8). Нестандартные
каталоги кешей задаются на машине через `UV_CACHE_DIR` и `HF_HOME`, не в проекте.

## 2. Один раз за сессию — предполётная проверка

В **интерактивном** PowerShell (env сохранится в окне):

```powershell
. .\scripts\preflight.ps1            # точка-пробел = dot-source
. .\scripts\preflight.ps1 -StartNeo4j  # + поднять Neo4j
```

Скрипт обнаруживает uv/настраивает UTF-8, доустанавливает `.venv` при необходимости,
проверяет связность Neo4j и печатает итог (`Preflight OK` либо список проблем).
Для нестандартных путей доступны `-UvDir` и `-HfHome`; они действуют только в
текущем процессе.
Вывод скрипта намеренно на английском и ASCII — PowerShell 5.1 читает `.ps1` в cp1251
без BOM, поэтому кириллица в коде скрипта сломала бы парсинг (см. таблицу ниже).

> Внутри инструментов агента dot-source бесполезен (env не переживает вызов) —
> там полагайся на префикс из п.1 в каждой команде. Скрипт — для интерактива и
> одноразовой диагностики готовности.

## 3. Git-worktree: пустой `.venv`

Каждый git-worktree имеет **свой** `.venv`, и у свежего он **пуст** → команды падают с
`program not found` (даже `pytest`/`onec-vecgraph`). Лечение — синхронизировать из lock:

```powershell
uv sync --frozen      # установить ровно по uv.lock, без сети и без правки lock
uv sync               # если lock устарел (нужна сеть)
```

`--frozen` не трогает `uv.lock` (важно: обычный `uv run` в пустом worktree может
до-резолвить и «раздуть» lock — не коммить такой шум).

## 4. Таблица: симптом → причина → фикс

| Симптом | Причина | Фикс |
|---|---|---|
| `program not found` / `uv не распознан` | `uv` не в PATH (свежая сессия) | установить `winget install astral-sh.uv`, обновить shell или передать `preflight.ps1 -UvDir <путь>` |
| `pytest`/`onec-vecgraph` → `program not found`, хотя `uv` есть | пустой/несинхр. `.venv` (git-worktree) | `uv sync --frozen` (см. п.3) |
| Кракозябры вместо кириллицы | консоль в cp1251 | UTF-8 из префикса п.1. Это **только отображение** — в Neo4j/JSON данные верны; для проверки пиши результат в JSON и читай файл |
| `.ps1` не парсится, «Unexpected token» на русских словах | PowerShell 5.1 читает `.ps1` как cp1251 без BOM | держи скрипты в ASCII **или** сохраняй `.ps1` с UTF-8 BOM (`Out-File -Encoding utf8`) |
| `pytest` собрал **0** тестов | в `pyproject` нет `testpaths` | `uv run pytest tests/` (явный путь) |
| `uv run` тянет пересборку и падает | правка `pyproject`/зависимостей → офлайн-ребилд (нет сети) | `uv run --no-sync onec-vecgraph …` |
| `NativeCommandError` в выводе при коде выхода 0 | PowerShell оборачивает stderr нативной команды (особенно при `2>&1`) | **не ошибка** — не редиректь stderr нативных exe; смотри exit code |
| Команда «зависла» после `vectorize`/`search` на GPU | torch на Windows виснет на выходе → CLI делает `os._exit(0)` после печати | это норма, данные закоммичены — не убивай преждевременно |
| Docker не подхватил новые env | `docker compose up -d` не пересоздаёт контейнер при смене env | `docker compose up -d --force-recreate` |
| Осиротевший `python` держит VRAM | зависший фоновый прогон | убить процесс через Task Manager (из неэлевированной сессии возможен Access denied) |
| `vector.similarity.cosine` падает | Neo4j < 5.18 (точный фильтрованный поиск) | на 5.26 ок; на старых — только индексный путь |
| Первый `vectorize`/`docinfo` долго «молчит» | качается модель (~1.2 ГБ) в пользовательский кеш или `HF_HOME` | подождать; при нехватке места задать машинный `HF_HOME` вне Git |
| Пустой результат поиска/графа вызовов | слой не построен для тенанта (а не «не найдено») | `uv run onec-vecgraph metrics --tenant-id <t>` — есть ли chunks/routines |
| OOM на GPU при `vectorize` | длинные последовательности/большой батч | `EMBEDDING_MAX_SEQ_LENGTH=256` + меньше `EMBEDDING_BATCH_SIZE` (`expandable_segments` на Windows игнорируется) |

## 5. Минимальная последовательность старта

```powershell
. .\scripts\preflight.ps1 -StartNeo4j      # окружение + Neo4j + health (интерактивно)
# далее операции — с префиксом п.1, по OPERATOR_PLAYBOOK.md:
uv run onec-vecgraph metrics --tenant-id demo     # дымовая проверка, что слой построен
```
