"""Таблица сравнения: инструменты onec-lite против компетентного grep-агента.

Что честно в этом замере (в отличие от первых версий харнеса):

* **настоящий токенизатор** (tiktoken cl100k_base), а не `len/3` — на русском тексте это
  ~1.96 симв/токен, то есть прокси занижал расход в полтора раза;
* **wire-payload MCP**: считается текст, который реально уходит клиенту (`mcp.call_tool`);
* **компетентный grep**: агенту разрешено то, что есть у настоящего агента — конвейеры
  (`grep -v` чтобы выбросить объявления, `sort|uniq -c|head` чтобы агрегировать в шелле),
  а не «вылить 549 строк в контекст»;
* **равный корпус**: rg ищет по всем источникам воркспейса, включая расширения;
* **два режима времени**: `real` — как в живой сессии (модель думает доль TTL, поэтому
  «грязный» git-набор перечитывается) и `b2b` — вызовы подряд, как в синтетике;
* **предикат приёмки** записан у каждого вопроса: обе ветки платят за ответ на ОДИН вопрос.

Запуск: uv run --no-sync python scripts/bench_tools_vs_grep.py [--runs 3] [--md out.md]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import time
from pathlib import Path

import tiktoken

# Замер должен видеть ВСЕ инструменты, а не только профиль по умолчанию: в `lean` часть
# тулов не публикуется (они дублируют rg/чтение файла), и вызов падал бы «Unknown tool».
os.environ.setdefault("ONEC_LITE_PROFILE", "full")

from onec_vecgraph.lite import code_intel, search
from onec_vecgraph.lite import server as lite_server
from onec_vecgraph.lite.workspace import Workspace

UT_ROOT = r"H:\1C\xml\GT\prod\ut\conf"
UT_EXTS = (
    r"H:\1C\xml\GT\prod\ut\битЕГАИС_УТ",
    r"H:\1C\xml\GT\prod\ut\дит_КонтурEDI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_ПретензииMMBI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_РасширениеАдаптацияУТ",
)
WS = "bench_tbl"
REPO = Path(UT_ROOT).parent
REF = "HEAD~5"
HOT = "ОбработкаЗаполнения"
NORM = "ДобавлениеРеквизитовФормы"
DOC = "ДИТ_Претензия"
CM = "ДИТ_ТранспортныеНазначения"
OVR = "ПриобретениеТоваровУслуг"  # 62 перехвата в расширениях — у вопроса есть ответ в обеих ветках

_ENC = tiktoken.get_encoding("cl100k_base")


def tok(text: str) -> int:
    return len(_ENC.encode(text))


def med(xs: list[float]) -> float:
    return round(statistics.median(xs), 3)


def _bash() -> str:
    """Путь к Git Bash. В PATH на Windows обычно лежит WSL-bash (system32\\bash.exe), который
    не понимает пути вида H:\\1C\\... — на нём ветка grep молча возвращала пустой вывод."""
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).is_file():
            return cand
    return "bash"


_BASH = _bash()


def sh(cmd: str) -> str:
    """Компетентный grep-агент: одна shell-строка с конвейером."""
    proc = subprocess.run([_BASH, "-c", cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if not proc.stdout and proc.stderr:
        return f"[stderr] {proc.stderr.strip()[:400]}"  # чтобы провал не выглядел как «0 токенов»
    return proc.stdout


def dirs(ws: Workspace) -> str:
    return " ".join(f'"{s.files_root}"' for s in ws.sources)


async def call(tool: str, args: dict) -> str:
    res = await lite_server.mcp.call_tool(tool, {**args, "workspace": WS})
    content = res[0] if isinstance(res, tuple) else res
    return "".join(getattr(c, "text", "") for c in content)


def obj_dirs(ws: Workspace, folder: str, name: str) -> str:
    """Каталоги объекта во ВСЕХ источниках — сужение для grep без масок.

    Маска вида `-g "*/Documents/Имя/*"` на абсолютных Windows-путях не срабатывает: ветка grep
    молча возвращала пустоту, а пустой ответ в таблице выглядел как «0 токенов» — то есть
    подтасовка меняла знак, но не исчезала."""
    return " ".join(f'"{s.files_root / folder / name}"' for s in ws.sources
                    if (s.files_root / folder / name).is_dir())


def _cold() -> None:
    """Честно ПЕРВЫЙ вопрос в сессии: свежий Workspace и погашенные кэши разбора.

    Двух ошибок здесь уже было. Сначала сбрасывался только `_DIRTY_CACHE`, и разобранные модули,
    строки, индекс перехватов и git-списки выживали. Потом выяснилось, что и этого мало: кэш
    листингов и объектов живёт в самом Workspace, а `main()` прогревал его ДО замера (1.8 с), так
    что «первый вопрос» на деле мерился вторым — отсюда фиктивные x687 у get_object и x3 у
    list_routines. Создаём Workspace заново, как это делает свежая сессия."""
    from onec_vecgraph.lite import gitview as _gv

    code_intel.clear_caches()
    _gv._FILE_LISTS.clear()  # noqa: SLF001
    _gv._repo_root.cache_clear()  # noqa: SLF001
    lite_server._WORKSPACES[WS] = Workspace(UT_ROOT, ext_roots=UT_EXTS)  # noqa: SLF001


def scenarios(ws: Workspace, rg: str) -> list[tuple]:
    D = dirs(ws)
    return [
        ("find_callers (горячий метод)", "места вызова с файлом и строкой, без объявлений",
         ("find_callers", {"routine_name": HOT, "max_results": 20}),
         f'"{rg}" -n --no-heading -e "\\b{HOT}\\s*\\(" {D} | grep -v -E ":[0-9]+:[[:space:]]*(Процедура|Функция)" | head -20'),
        ("find_callers (сводка)", "сколько всего и по каким объектам",
         ("find_callers", {"routine_name": HOT, "summary_only": True}),
         f'"{rg}" -n --no-heading -e "\\b{HOT}\\s*\\(" {D} | grep -v -E ":[0-9]+:[[:space:]]*(Процедура|Функция)" | sed -E "s#.*(src[/\\\\][^/\\\\]+[/\\\\][^/\\\\]+).*#\\1#" | sort | uniq -c | sort -rn | head -20'),
        ("find_callers (обычный метод)", "места вызова",
         ("find_callers", {"routine_name": NORM, "max_results": 20}),
         f'"{rg}" -n --no-heading -e "\\b{NORM}\\s*\\(" {D} | grep -v -E ":[0-9]+:[[:space:]]*(Процедура|Функция)"'),
        ("find_routine (объявления)", "где объявлен метод",
         ("find_routine", {"routine_name": NORM, "max_results": 10}),
         f'"{rg}" -n --no-heading -e "^\\s*(Процедура|Функция)\\s+{NORM}\\s*\\(" {D} | head -10'),
        ("get_object (реквизиты+обязательность)", "состав реквизитов с типами и обязательностью",
         ("get_object", {"kind": "Document", "name": DOC}),
         # Греп по СОБСТВЕННЫМ .mdo объекта: прежняя команда шла по всей конфигурации и
         # фильтровала по подстроке имени — в выдачу попадали чужие объекты, а состава
         # реквизитов запрошенного там не было, то есть ветка grep на вопрос не отвечала.
         f'"{rg}" -n -g "*.mdo" -e "<name>" -e "<types>" -e "fillChecking" '
         f'{obj_dirs(ws, "Documents", DOC)} | head -200'),
        ("read_routine (тело метода)", "текст одной рутины",
         ("read_routine", {"routine_name": NORM}),
         # grep обязан ПРОЧИТАТЬ тело: координаты объявления — не ответ на «текст рутины».
         # Прежние 92 токена против 1850 у MCP сравнивали ссылку с содержимым.
         f'f=$("{rg}" -l -e "^\\s*(Процедура|Функция)\\s+{NORM}" {D} | head -1); '
         f'awk \'/^[[:space:]]*(Процедура|Функция)[[:space:]]+{NORM}/,'
         f'/^[[:space:]]*(КонецПроцедуры|КонецФункции)/\' "$f"'),
        # Объект с РЕАЛЬНЫМИ перехватами (62 в расширениях) и grep, суженный на тот же объект.
        # Раньше здесь стоял объект без перехватов, а команда grep не фильтровала по объекту:
        # обе ветки не отвечали на вопрос, и строка давала MCP фиктивные x43 по токенам.
        ("find_overrides (перехваты объекта)", "что переопределяют расширения у объекта",
         ("find_overrides", {"kind": "Document", "name": OVR, "max_results": 20}),
         f'"{rg}" -n --no-heading -e "^\\s*&(Вместо|Перед|После|ИзменениеИКонтроль)" '
         f'{obj_dirs(ws, "Documents", OVR)} | head -20'),
        ("writes_to (движения документа)", "в какие регистры пишет документ",
         ("writes_to", {"document": DOC}),
         f'"{rg}" -n -g "*.mdo" -e "registerRecords" {D} | grep -i "{DOC}" | head -20'),
        ("changed_objects (что изменено)", "какие объекты метаданных изменены с ref",
         ("changed_objects", {"ref": REF}),
         f'git -C "{REPO}" -c safe.directory=* -c core.quotepath=off diff --name-status {REF}'),
        ("review_set (ревью-набор)", "затронутые рутины и их вызывающие",
         ("review_set", {"ref": REF}),
         f'git -C "{REPO}" -c safe.directory=* -c core.quotepath=off diff --name-status {REF}; '
         f'git -C "{REPO}" -c safe.directory=* diff -U0 {REF} -- "*.bsl" | grep -E "^@@|^\\+\\+\\+" | head -60'),
        ("fts_search (NL «где считается»)", "ранжированный ответ на человеческий запрос",
         ("fts_search", {"query": "проверка заполнения транспортного назначения", "limit": 10}),
         f'"{rg}" -n --no-heading -e "ПроверитьЗаполнение" -e "ТранспортноеНазначение" {D} | head -30'),
        ("list_routines (состав модуля)", "какие методы есть в модуле",
         ("list_routines", {"kind": "CommonModule", "name": CM, "max_results": 40}),
         f'"{rg}" -n --no-heading -e "^\\s*(Процедура|Функция)" '
         f'{obj_dirs(ws, "CommonModules", CM)} | head -40'),
        ("find_type_usages (где тип)", "где используется тип объекта",
         ("find_type_usages", {"kind": "Document", "name": DOC, "max_results": 30}),
         f'"{rg}" -n -g "*.mdo" -g "*.form" -e "Document(Ref|Object)\\.{DOC}\\b" {D} | head -30'),
    ]


async def run(ws: Workspace, runs: int) -> list[dict]:
    rg = search.rg_path() or "rg"
    rows: list[dict] = []
    for title, predicate, (tool, args), cmd in scenarios(ws, rg):
        lt_real: list[float] = []
        lt_b2b: list[float] = []
        text = ""
        for i in range(runs):
            _cold()  # режим «живой сессии»: гасим ВСЕ кэши, а не только грязный набор
            t0 = time.perf_counter()
            text = await call(tool, args)
            lt_real.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            await call(tool, args)           # сразу второй вызов — режим «подряд»
            lt_b2b.append(time.perf_counter() - t0)
        gt: list[float] = []
        gout = ""
        for _ in range(runs):
            t0 = time.perf_counter()
            gout = sh(cmd)
            gt.append(time.perf_counter() - t0)
        mt, gtok = tok(text), tok(gout)
        rows.append({
            "tool": title, "predicate": predicate,
            "mcp_tok": mt, "grep_tok": gtok,
            "mcp_real_s": med(lt_real), "mcp_b2b_s": med(lt_b2b), "grep_s": med(gt),
            "tok_ratio": round(gtok / mt, 2) if mt else None,
            "speed_real": round(med(gt) / med(lt_real), 2) if med(lt_real) else None,
        })
        r = rows[-1]
        print(f"  {title:<38} MCP {r['mcp_tok']:>6}т/{r['mcp_real_s']:>6.3f}s | "
              f"grep {r['grep_tok']:>6}т/{r['grep_s']:>6.3f}s | "
              f"токены x{r['tok_ratio']} скорость x{r['speed_real']}")
    return rows


def to_md(rows: list[dict]) -> str:
    out = ["| Инструмент | MCP токенов | grep токенов | Токены (>1 = MCP дешевле) | "
           "MCP с, живой темп | MCP с, подряд | grep с | Скорость (>1 = MCP быстрее) |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['tool']} | {r['mcp_tok']} | {r['grep_tok']} | {r['tok_ratio']} | "
                   f"{r['mcp_real_s']} | {r['mcp_b2b_s']} | {r['grep_s']} | {r['speed_real']} |")
    tot_m = sum(r["mcp_tok"] for r in rows)
    tot_g = sum(r["grep_tok"] for r in rows)
    out.append(f"| **Сумма по 13 вопросам** | **{tot_m}** | **{tot_g}** | "
               f"**{round(tot_g / tot_m, 2)}** | | | | |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--md", default="")
    args = ap.parse_args()
    ws = Workspace(UT_ROOT, ext_roots=UT_EXTS)
    for s in ws.sources:
        ws.listing(s)
    lite_server._WORKSPACES[WS] = ws  # noqa: SLF001
    print(f"стенд: УТ ({len(ws.sources)} источников), tiktoken cl100k, runs={args.runs}, ref={REF}")
    rows = asyncio.run(run(ws, args.runs))
    md = to_md(rows)
    print("\n" + md)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
