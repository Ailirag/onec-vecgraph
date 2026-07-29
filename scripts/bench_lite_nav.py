"""Замер сценариев навигации по коду 1С: onec-lite против сырого grep/rg.

Меряем то, что реально платит агент: **время** и **объём выдачи** (прокси токенов), на живой
рабочей копии УТ и на символах из настоящих коммитов релиза (ONE-4679, ONE-4545).

Честность сравнения: grep-ветка считается по «полному пути до ответа». rg отдаёт
непроверенные попадания (комментарии, объявления, одноимённые методы), поэтому агенту нужно
дочитать контекст вокруг каждого — этот доп. объём включён в baseline (_CTX строк на попадание,
не более _CTX_HITS попаданий). Ветка onec-lite считается по своему JSON-ответу: он уже
проверен парсером и структурен, дочитывать нечего.

Запуск:  uv run python scripts/bench_lite_nav.py [--json <файл>]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from onec_vecgraph.lite import code_intel, fts, gitview, metaview, search
from onec_vecgraph.lite.workspace import Workspace, read_text

# --------------------------------------------------------------------------- #
# Стенд: живая рабочая копия УТ (база + 4 расширения) и цели из коммитов релиза
# --------------------------------------------------------------------------- #

UT_ROOT = r"H:\1C\xml\GT\prod\ut\conf"
UT_EXTS = (
    r"H:\1C\xml\GT\prod\ut\битЕГАИС_УТ",
    r"H:\1C\xml\GT\prod\ut\дит_КонтурEDI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_ПретензииMMBI",
    r"H:\1C\xml\GT\prod\ut\ДИТ_РасширениеАдаптацияУТ",
)
UT_REPO = Path(r"H:\1C\xml\GT\prod\ut")

DOC = "ДИТ_Претензия"                    # ONE-4679: +СтатусПрокси, JOIN Сторно, переименование
CM = "ДИТ_ТранспортныеНазначения"        # ONE-4545: убрали проверку заполненности
SVC = "ДИТ_TMSIntegration"               # ONE-4545: HTTP-сервис
REF = "HEAD~5"                            # диапазон релиза для git-сценариев

_CTX = 10        # строк контекста, которые агент дочитывает вокруг непроверенного grep-хита
_CTX_HITS = 20   # сколько хитов агент реально дочитывает, прежде чем сдаться
_CHARS_PER_TOKEN = 3.0  # прокси для смешанного русского текста/кода в UTF-8


def _tok(payload: object) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return int(len(text) / _CHARS_PER_TOKEN)


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def _rg(args: list[str]) -> str:
    """Сырой rg по рабочей копии (как это делает агент через Grep/Bash)."""
    exe = search.rg_path()
    if not exe:
        return ""
    proc = subprocess.run([exe, *args, str(Path(UT_ROOT) / "src")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout


def _rg_with_context(pattern: str) -> str:
    """grep-путь целиком: попадания + дочитанный контекст вокруг первых _CTX_HITS."""
    out = _rg(["-n", "--no-heading", pattern])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    payload = [out]
    for ln in lines[:_CTX_HITS]:
        m = re.match(r"^(.*?):(\d+):", ln)
        if not m:
            continue
        path, no = Path(m.group(1)), int(m.group(2))
        try:
            body = read_text(path).splitlines()
        except OSError:
            continue
        payload.append("\n".join(body[max(0, no - 1 - _CTX): no + _CTX]))
    return "\n".join(payload)


# --------------------------------------------------------------------------- #
# Сценарии: (ключ, вопрос ревьюера, ветка onec-lite, ветка grep)
# --------------------------------------------------------------------------- #

def build_scenarios(ws: Workspace, routine: str) -> list[dict]:
    def lite_callers():
        return code_intel.find_callers(ws, routine, max_results=100)

    def grep_callers():
        return _rg_with_context(rf"\b{re.escape(routine)}\s*\(")

    def lite_object():
        obj = ws.find_object("Document", DOC)[1]
        src = ws.find_object("Document", DOC)[0]
        parsed = ws.parse_object(src, obj)
        return {
            "fqn": parsed.fqn, "synonym": parsed.synonym,
            "attributes": [{"name": f.name, "type": f.type_text,
                            **({"required": True} if f.fill_checking == "ShowError" else {})}
                           for f in parsed.fields],
            "tabular": [t.name for t in parsed.tabular],
            "forms": [f.name for f in parsed.forms],
        }

    def grep_object():
        """Без lite агент читает .mdo целиком (структуру иначе не получить)."""
        src, ref = ws.find_object("Document", DOC)[0], ws.find_object("Document", DOC)[1]
        return read_text(ref[1])

    def lite_overrides():
        return code_intel.find_overrides(ws, kind="Document", name=DOC)

    def grep_overrides():
        return _rg_with_context(r"^\s*&(Вместо|Перед|После|ИзменениеИКонтроль)")

    def lite_type_usages():
        return code_intel.type_usages(ws, "Document", DOC, max_results=100)

    def grep_type_usages():
        return _rg_with_context(rf"(DocumentRef|DocumentObject)\.{re.escape(DOC)}\b")

    def lite_read_routine():
        src, ref, _also, err = ws.find_object("CommonModule", CM)
        if err:
            return {"error": err}
        path, _msg = ws.module_path(src, "CommonModule", CM, "Module")
        rt = code_intel.find_in_module(path, routine) if path else None
        return {"routine": routine, "body": code_intel.routine_body(path, rt) if rt else ""}

    def grep_read_routine():
        """Без lite границы рутины неизвестны — агент читает модуль целиком."""
        src, _ref, _also, err = ws.find_object("CommonModule", CM)
        if err:
            return ""
        path, _msg = ws.module_path(src, "CommonModule", CM, "Module")
        return read_text(path) if path else ""

    def lite_service():
        src, ref, _a, err = ws.find_object("HTTPService", SVC)
        if err:
            return {"error": err}
        return metaview.parse_http_service(ref[1]) or {}

    def grep_service():
        src, ref, _a, err = ws.find_object("HTTPService", SVC)
        return read_text(ref[1]) if not err else ""

    def lite_changed():
        return gitview.changed_objects(ws, ref=REF)

    def grep_changed():
        proc = subprocess.run(
            ["git", "-C", str(UT_REPO), "-c", "safe.directory=*", "-c", "core.quotepath=off",
             "diff", "--name-status", REF],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return proc.stdout

    def lite_review():
        return gitview.review_set(ws, ref=REF, max_callers=8)

    def grep_review():
        """grep-эквивалента нет: агент берёт полный дифф и сам ищет границы рутин."""
        proc = subprocess.run(
            ["git", "-C", str(UT_REPO), "-c", "safe.directory=*", "-c", "core.quotepath=off",
             "diff", "-U10", REF, "--", "*.bsl"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return proc.stdout

    def lite_fts():
        return fts.index_for(ws).search("проверка заполнения транспортного назначения", limit=20)

    def grep_fts():
        return _rg_with_context(r"(ПроверкаЗаполнения|ПроверитьЗаполнение|ТранспортноеНазначение)")

    return [
        {"key": "find_callers", "q": f"Кто вызывает {routine}?",
         "lite": lite_callers, "grep": grep_callers},
        {"key": "get_object", "q": f"Структура Document.{DOC} (реквизиты, обязательность)",
         "lite": lite_object, "grep": grep_object},
        {"key": "find_overrides", "q": f"Что переопределяют расширения у Document.{DOC}?",
         "lite": lite_overrides, "grep": grep_overrides},
        {"key": "find_type_usages", "q": f"Где используется тип Document.{DOC}?",
         "lite": lite_type_usages, "grep": grep_type_usages},
        {"key": "read_routine", "q": f"Тело рутины {routine} в CommonModule.{CM}",
         "lite": lite_read_routine, "grep": grep_read_routine},
        {"key": "get_service", "q": f"Структура HTTPService.{SVC}",
         "lite": lite_service, "grep": grep_service},
        {"key": "changed_objects", "q": f"Какие объекты изменены с {REF}?",
         "lite": lite_changed, "grep": grep_changed},
        {"key": "review_set", "q": f"Ревью-набор: рутины+вызывающие с {REF}",
         "lite": lite_review, "grep": grep_review},
        {"key": "fts_search", "q": "«проверка заполнения транспортного назначения»",
         "lite": lite_fts, "grep": grep_fts},
    ]


def _pick_routine(ws: Workspace) -> str:
    """Экспортная рутина из модуля, затронутого ONE-4545 — реальная цель ревью."""
    src, _ref, _also, err = ws.find_object("CommonModule", CM)
    if err:
        return "ПроверитьЗаполнение"
    path, _msg = ws.module_path(src, "CommonModule", CM, "Module")
    if not path:
        return "ПроверитьЗаполнение"
    exported = [r.name for r in code_intel.routines_of(path) if r.export]
    return exported[0] if exported else "ПроверитьЗаполнение"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    ws = Workspace(UT_ROOT, ext_roots=UT_EXTS)
    rg = search.rg_path()
    routine = _pick_routine(ws)
    print(f"стенд: УТ, источников={len(ws.sources)}, rg={'да' if rg else 'НЕТ (python-фолбэк)'}, "
          f"рутина-цель={routine}, ref={REF}")

    # прогрев: листинги/индекс — как у долгоживущего http-сервера (иначе мерим холодный старт)
    for s in ws.sources:
        ws.listing(s)
    built = fts.index_for(ws).status().get("built")
    print(f"FTS индекс: {'построен' if built else 'НЕ построен (fts-сценарий деградирует)'}")

    rows = []
    for sc in build_scenarios(ws, routine):
        lite_out, lite_s = _timed(sc["lite"])
        grep_out, grep_s = _timed(sc["grep"])
        lite_t, grep_t = _tok(lite_out), _tok(grep_out)
        rows.append({
            "scenario": sc["key"], "question": sc["q"],
            "lite_seconds": round(lite_s, 3), "grep_seconds": round(grep_s, 3),
            "lite_tokens": lite_t, "grep_tokens": grep_t,
            "speedup": round(grep_s / lite_s, 2) if lite_s > 0 else None,
            "token_ratio": round(grep_t / lite_t, 2) if lite_t > 0 else None,
        })
        print(f"  {sc['key']:<18} lite {lite_s:6.2f}s/{lite_t:>7}tok | "
              f"grep {grep_s:6.2f}s/{grep_t:>7}tok")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
