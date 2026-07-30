"""Сквозная проверка ответов onec-lite на НЕЗАВИСИМЫХ коммитах и задачах.

Берём реальные коммиты релиза (по умолчанию — не те, на которых велась разработка), достаём
затронутые объекты и рутины НЕЙТРАЛЬНО через git, и проверяем инвариантами:

* найденное git'ом видно инструментам (объект существует, рутина объявлена);
* счётчики означают то, что написано: сумма by_object == полный счёт; отданные строки не
  превышают счёт; при truncated=true есть чем добрать (offset/полный счёт);
* индексный путь и живой скан дают ОДНО И ТО ЖЕ множество вызывающих;
* rg не находит того, что инструмент пропустил (перекрёстная проверка полноты по тексту).

Любое нарушение печатается как FAIL с числами — это дефект, а не «особенность».

Запуск: uv run --no-sync python scripts/validate_lite_answers.py [--commits h1,h2]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from onec_vecgraph.lite import code_intel, fts, search
from onec_vecgraph.lite.workspace import Workspace, read_text

UT_ROOT = r"H:\1C\xml\GT\prod\ut\conf"
UT_EXTS = (
    r"H:\1C\xml\GT\prod\ut\битЕГАИС_УТ",
    r"H:\1C\xml\GT\prod\ut\дит_КонтурEDI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_ПретензииMMBI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_РасширениеАдаптацияУТ",
)
REPO = Path(UT_ROOT).parent
# Независимый набор: ONE-2623 (41 файл), ONE-4502 (22), ONE-4620 (9) — разработка шла не на них.
DEFAULT_COMMITS = ["9a39a83e3c", "54f5431ee1", "7b2302c5e7"]

_DECL_RE = re.compile(r"^\s*(?:Процедура|Функция)\s+([A-Za-zА-Яа-яЁё_][\w]*)", re.IGNORECASE)


def _git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "-c", "safe.directory=*", "-c", "core.quotepath=off", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout


def touched(commit: str) -> tuple[list[str], list[str]]:
    """(изменённые .bsl относительно репозитория, имена рутин из добавленных строк)."""
    files = [ln.strip() for ln in _git(["show", "--name-only", "--format=", commit]).splitlines()
             if ln.strip().endswith(".bsl")]
    routines: list[str] = []
    for ln in _git(["show", "-U12", "--format=", commit, "--", "*.bsl"]).splitlines():
        m = _DECL_RE.match(ln[1:] if ln[:1] in "+- " else ln)
        if m:
            routines.append(m.group(1))
    return files, list(dict.fromkeys(routines))


def rg_count(ws: Workspace, pattern: str) -> int:
    exe = search.rg_path()
    if not exe:
        return -1
    dirs = [str(s.files_root) for s in ws.sources]
    proc = subprocess.run([exe, "-c", "--no-heading", "-e", pattern, *dirs],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return sum(int(ln.rsplit(":", 1)[1]) for ln in proc.stdout.splitlines() if ":" in ln)


def index_audit(ws: Workspace) -> list[str]:
    """Полная сверка индекса символов с живым разбором: числа И координаты.

    Самая сильная проверка полноты: один проход по всем .bsl против содержимого таблицы
    symbols. Ловит и пропущенные файлы (папка вне обхода), и потерянные рутины (парсер), и
    расхождение координат (индекс отстал), не полагаясь на выборочные запросы."""
    from onec_vecgraph.bsl.parser import parse_module

    fails: list[str] = []
    idx = fts.index_for(ws)
    if not idx.has_symbols():
        return ["индекс символов не построен — сверять нечего"]
    import sqlite3
    con = sqlite3.connect(str(fts.db_path_for(ws)))
    try:
        indexed = {(p, n, s) for p, n, s in
                   con.execute("SELECT path, name, start_line FROM symbols")}
    finally:
        con.close()

    live: set[tuple] = set()
    files = 0
    for src in ws.sources:
        for path in ws.bsl_files(src, fresh=True):
            files += 1
            try:
                for rt in parse_module(read_text(path)):
                    live.add((str(path), rt.name, rt.start_line))
            except OSError:
                continue
    only_live = live - indexed
    only_index = indexed - live
    print(f"    файлов разобрано={files} живых рутин={len(live)} в индексе={len(indexed)}")
    if only_live:
        sample = list(only_live)[:3]
        fails.append(f"индекс НЕ содержит {len(only_live)} живых рутин, напр.: {sample}")
    if only_index:
        sample = list(only_index)[:3]
        fails.append(f"в индексе {len(only_index)} рутин, которых нет в живом разборе, "
                     f"напр.: {sample}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", default=",".join(DEFAULT_COMMITS))
    ap.add_argument("--per-commit", type=int, default=6, help="рутин на коммит")
    ap.add_argument("--index-audit", action="store_true",
                    help="полная сверка symbols с живым разбором (минуты)")
    args = ap.parse_args()

    ws = Workspace(UT_ROOT, ext_roots=UT_EXTS)
    for s in ws.sources:
        ws.listing(s)
    idx = fts.index_for(ws)
    print(f"стенд: УТ, источников={len(ws.sources)}, индекс symbols={idx.has_symbols()}, "
          f"rg={'да' if search.rg_path() else 'нет'}")

    fails: list[str] = []
    checked = 0
    if args.index_audit:
        print("\n=== полная сверка индекса с живым разбором")
        fails.extend(index_audit(ws))
    for commit in [c.strip() for c in args.commits.split(",") if c.strip()]:
        subject = _git(["log", "-1", "--format=%s", commit]).strip()[:60]
        files, routines = touched(commit)
        print(f"\n=== {commit} {subject}\n    .bsl файлов={len(files)}, рутин из диффа={len(routines)}")
        for rt_name in routines[: args.per_commit]:
            checked += 1
            # 1) объявление должно находиться
            decl = code_intel.find_declarations(ws, rt_name, max_results=5)
            if decl.get("declaration_count", 0) < 1:
                fails.append(f"{commit}: рутина {rt_name} из коммита НЕ найдена "
                             f"(engine={decl.get('engine')})")
                continue
            # 2) счётчики согласованы
            callers = code_intel.find_callers(ws, rt_name, max_results=20)
            total = callers.get("call_rows_total")
            shown = callers.get("match_count", 0)
            by_sum = sum(x["count"] for x in callers.get("by_object", []))
            if total is not None:
                if shown > total:
                    fails.append(f"{commit}/{rt_name}: показано {shown} > всего {total}")
                # Сводка может быть обрезана до топ-N — тогда сумма ДОЛЖНА быть меньше полного
                # счёта, но обрезка обязана быть заявлена, а показанная часть — сходиться с
                # by_object_rows_shown. Иначе агент примет верхушку за всё распределение.
                if callers.get("by_object_truncated"):
                    if by_sum > total or by_sum != callers.get("by_object_rows_shown"):
                        fails.append(f"{commit}/{rt_name}: обрезанная сводка не сходится "
                                     f"({by_sum} против {callers.get('by_object_rows_shown')}, "
                                     f"всего {total})")
                elif by_sum != total:
                    fails.append(f"{commit}/{rt_name}: сумма by_object {by_sum} != всего {total}")
                if callers.get("truncated") and total <= shown:
                    fails.append(f"{commit}/{rt_name}: truncated при total<=shown")
            # 3) паритет индекс/скан
            if idx.has_symbols():
                orig = fts.FtsIndex.has_symbols
                fts.FtsIndex.has_symbols = lambda self: False  # noqa: ARG005
                try:
                    code_intel.clear_caches()
                    scan = code_intel.find_callers(ws, rt_name, max_results=10_000)
                finally:
                    fts.FtsIndex.has_symbols = orig
                    code_intel.clear_caches()
                idx_all = code_intel.find_callers(ws, rt_name, max_results=10_000)
                a, b = scan.get("match_count", 0), idx_all.get("match_count", 0)
                if a != b:
                    fails.append(f"{commit}/{rt_name}: скан {a} != индекс {b}")
            # 4) перекрёстная проверка по тексту: rg не должен видеть вызовы там, где мы 0
            if (total or 0) == 0:
                hits = rg_count(ws, rf"\b{re.escape(rt_name)}\s*\(")
                decls = decl.get("declaration_count", 0)
                if hits > decls + 2:  # объявления + запас на комментарии
                    fails.append(f"{commit}/{rt_name}: инструмент 0 вызовов, а rg видит "
                                 f"{hits} вхождений при {decls} объявлениях")
            print(f"    {rt_name:<44} объявлений={decl['declaration_count']:<5} "
                  f"вызовов={total if total is not None else '—'} показано={shown}")

    print(f"\nпроверено рутин: {checked}; нарушений: {len(fails)}")
    for f in fails:
        print(f"  FAIL {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
