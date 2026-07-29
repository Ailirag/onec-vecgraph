"""Замер навигации по коду 1С: onec-lite против КОМПЕТЕНТНОГО grep-агента, по проводу MCP.

Отличия от первой версии харнеса (`bench_lite_nav.py`), появившиеся после разбора методики:

* **равный корпус** — rg ищет по ВСЕМ источникам воркспейса (база + расширения), как и lite;
  раньше baseline шёл только по базе и «выигрывал» за счёт того, что просто не видел половины;
* **компетентный baseline** — агент с Grep не читает файлы целиком: он делает `rg -c` (счёт),
  `rg -n` с ограничением вывода и адресные чтения окон, а не `cat` модуля на 7 МБ. Соломенные
  чучела («прочитать весь .mdo») убраны;
* **реальные wire-токены** — ответ lite измеряется тем, что действительно уходит клиенту
  (`mcp.call_tool` → text content), а не компактным `json.dumps`; это на 20–40% больше;
* **N прогонов, медиана, cold/warm** — один прогон на большом репозитории шумит;
* **предикат приёмки** — у каждого сценария записано, что считается ответом на вопрос, и обе
  ветки тарифицируются до его выполнения.

Запуск: uv run --no-sync python scripts/bench_lite_wire.py [--runs 5] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

from onec_vecgraph.lite import code_intel, search
from onec_vecgraph.lite import server as lite_server
from onec_vecgraph.lite.workspace import Workspace, read_text

UT_ROOT = r"H:\1C\xml\GT\prod\ut\conf"
UT_EXTS = (
    r"H:\1C\xml\GT\prod\ut\битЕГАИС_УТ",
    r"H:\1C\xml\GT\prod\ut\дит_КонтурEDI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_ПретензииMMBI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_РасширениеАдаптацияУТ",
)
WS_NAME = "bench_wire"
REF = "HEAD~5"

HOT = "ОбработкаЗаполнения"          # «горячий» метод: 549 файлов-кандидатов текстом
COLD_RT = "ДобавлениеРеквизитовФормы"  # обычный метод из модуля, правленного в ONE-4545
DOC = "ДИТ_Претензия"                  # объект из ONE-4679
CM = "ДИТ_ТранспортныеНазначения"      # общий модуль из ONE-4545

_GREP_LINE_CAP = 250   # столько строк максимум забирает агент из одного Grep
_READ_WINDOW = 20      # окно адресного чтения вокруг попадания
_READ_HITS = 10        # сколько попаданий агент реально дочитывает
_CPT = 3.0             # символов на токен (смешанный русский текст/код в UTF-8)


def _tok(text: str) -> int:
    return int(len(text) / _CPT)


def _median(xs: list[float]) -> float:
    return round(statistics.median(xs), 3)


# --------------------------------------------------------------------------- #
# Ветка grep: равный корпус, компетентная стратегия
# --------------------------------------------------------------------------- #

def _search_dirs(ws: Workspace) -> list[str]:
    return [str(s.files_root) for s in ws.sources]


def _rg(ws: Workspace, args: list[str]) -> str:
    exe = search.rg_path()
    if not exe:
        return ""
    proc = subprocess.run([exe, *args, *_search_dirs(ws)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.stdout


def _rg_count(ws: Workspace, pattern: str) -> str:
    """Первый шаг компетентного агента: сколько попаданий и где (без содержимого)."""
    return _rg(ws, ["-c", "--no-heading", "-e", pattern])


def _rg_lines(ws: Workspace, pattern: str, cap: int = _GREP_LINE_CAP) -> list[str]:
    out = _rg(ws, ["-n", "--no-heading", "-m", "5", "-e", pattern])
    return [ln for ln in out.splitlines() if ln.strip()][:cap]


def _read_windows(lines: list[str], hits: int = _READ_HITS) -> str:
    """Адресные чтения окон вокруг попаданий — так агент проверяет, что это вызов."""
    chunks: list[str] = []
    for ln in lines[:hits]:
        m = re.match(r"^(.*?):(\d+):", ln)
        if not m:
            continue
        try:
            body = read_text(Path(m.group(1))).splitlines()
        except OSError:
            continue
        no = int(m.group(2))
        chunks.append("\n".join(body[max(0, no - 1 - _READ_WINDOW // 2): no + _READ_WINDOW // 2]))
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# Сценарии: предикат приёмки + обе ветки
# --------------------------------------------------------------------------- #

async def _lite(tool: str, args: dict) -> str:
    """Реальный вызов тула через MCP — измеряем то, что уходит клиенту."""
    res = await lite_server.mcp.call_tool(tool, {**args, "workspace": WS_NAME})
    content = res[0] if isinstance(res, tuple) else res
    return "".join(getattr(c, "text", "") for c in content)


def _grep_callers(ws: Workspace, routine: str) -> str:
    """Предикат: список мест вызова с файлом и строкой, без объявлений и комментариев.
    Компетентный путь: счёт -> строки (cap) -> адресные окна для проверки."""
    pattern = rf"\b{re.escape(routine)}\s*\("
    counts = _rg_count(ws, pattern)
    lines = _rg_lines(ws, pattern)
    return counts + "\n" + "\n".join(lines) + "\n" + _read_windows(lines)


def _grep_object_structure(ws: Workspace, kind: str, name: str) -> str:
    """Предикат: состав реквизитов объекта с типами и признаком обязательности.
    Компетентный путь: адресные grep-выборки из .mdo вместо чтения файла целиком."""
    src, ref, _a, err = ws.find_object(kind, name)
    if err:
        return ""
    meta = ref[1]
    exe = search.rg_path()
    if not exe:
        return read_text(meta)
    proc = subprocess.run(
        [exe, "-n", "-e", "<name>", "-e", "<types>", "-e", "fillChecking", "-e", "<synonym>",
         str(meta)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout


def _grep_routine_body(ws: Workspace, kind: str, name: str, routine: str) -> str:
    """Предикат: текст одной рутины. Компетентный путь: найти строку объявления и прочитать
    окно, а не весь модуль (модули 1С не влезают в лимит Read)."""
    src, _ref, _a, err = ws.find_object(kind, name)
    if err:
        return ""
    path, _msg = ws.module_path(src, kind, name, "Module")
    if not path:
        return ""
    exe = search.rg_path()
    if not exe:
        return read_text(path)
    proc = subprocess.run(
        [exe, "-n", "-e", rf"^\s*(Процедура|Функция)\s+{re.escape(routine)}\s*\(", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    hit = proc.stdout.strip().split(":", 1)
    if not hit or not hit[0].isdigit():
        return proc.stdout
    start = int(hit[0])
    body = read_text(path).splitlines()
    # агент не знает границы рутины -> берёт запас в 120 строк
    return "\n".join(body[start - 1: start + 119])


def _grep_changed(repo: Path) -> str:
    """Предикат: какие объекты метаданных изменены с ref. Компетентный путь: --name-status
    (не полный дифф) — но объекты агент должен вывести из путей сам."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "safe.directory=*", "-c", "core.quotepath=off",
         "diff", "--name-status", REF],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout


async def run(ws: Workspace, runs: int) -> list[dict]:
    repo = Path(UT_ROOT).parent
    scenarios = [
        ("find_callers.hot", f"Кто вызывает {HOT} (горячий метод)?",
         ("find_callers", {"routine_name": HOT, "max_results": 20}),
         lambda: _grep_callers(ws, HOT)),
        ("find_callers.hot.summary", f"Сколько и где вызывается {HOT} (сводка)?",
         ("find_callers", {"routine_name": HOT, "summary_only": True}),
         lambda: _rg_count(ws, rf"\b{re.escape(HOT)}\s*\(")),
        ("find_callers.normal", f"Кто вызывает {COLD_RT}?",
         ("find_callers", {"routine_name": COLD_RT, "max_results": 20}),
         lambda: _grep_callers(ws, COLD_RT)),
        ("get_object", f"Состав реквизитов Document.{DOC} (типы, обязательность)",
         ("get_object", {"kind": "Document", "name": DOC}),
         lambda: _grep_object_structure(ws, "Document", DOC)),
        ("read_routine", f"Тело рутины {COLD_RT}",
         ("read_routine", {"routine_name": COLD_RT}),
         lambda: _grep_routine_body(ws, "CommonModule", CM, COLD_RT)),
        ("changed_objects", f"Какие объекты изменены с {REF}?",
         ("changed_objects", {"ref": REF}),
         lambda: _grep_changed(repo)),
        ("review_set", f"Что затронуто правками с {REF} (рутины + вызывающие)?",
         ("review_set", {"ref": REF}),
         lambda: _grep_changed(repo) + _grep_callers(ws, COLD_RT)),
    ]

    rows: list[dict] = []
    for key, question, (tool, args), grep_fn in scenarios:
        lite_times: list[float] = []
        lite_text = ""
        for i in range(runs):
            if i == 0:
                code_intel.clear_caches()  # cold-прогон честно платит за разбор
            t0 = time.perf_counter()
            lite_text = await _lite(tool, args)
            lite_times.append(time.perf_counter() - t0)
        grep_times: list[float] = []
        grep_text = ""
        for _ in range(runs):
            t0 = time.perf_counter()
            grep_text = grep_fn()
            grep_times.append(time.perf_counter() - t0)
        row = {
            "scenario": key, "question": question,
            "lite_cold_s": round(lite_times[0], 3),
            "lite_warm_s": _median(lite_times[1:] or lite_times),
            "grep_s": _median(grep_times),
            "lite_wire_tokens": _tok(lite_text),
            "grep_tokens": _tok(grep_text),
        }
        row["speedup_warm"] = (round(row["grep_s"] / row["lite_warm_s"], 2)
                               if row["lite_warm_s"] > 0 else None)
        row["token_ratio"] = (round(row["grep_tokens"] / row["lite_wire_tokens"], 2)
                              if row["lite_wire_tokens"] > 0 else None)
        rows.append(row)
        print(f"  {key:<26} lite {row['lite_warm_s']:6.3f}s (cold {row['lite_cold_s']:5.2f}s) "
              f"{row['lite_wire_tokens']:>6}tok | grep {row['grep_s']:6.3f}s "
              f"{row['grep_tokens']:>7}tok | x{row['speedup_warm']} / x{row['token_ratio']}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    ws = Workspace(UT_ROOT, ext_roots=UT_EXTS)
    for s in ws.sources:
        ws.listing(s)
    lite_server._WORKSPACES[WS_NAME] = ws  # noqa: SLF001 - стенд, а не прод-путь
    print(f"стенд: УТ, источников={len(ws.sources)}, rg={'да' if search.rg_path() else 'НЕТ'}, "
          f"runs={args.runs}, ref={REF}")
    rows = asyncio.run(run(ws, args.runs))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
