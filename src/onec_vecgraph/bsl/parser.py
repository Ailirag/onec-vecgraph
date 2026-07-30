"""Lightweight, portable BSL parser (no native deps).

Extracts procedures/functions and their call sites from a 1C module. This is a
heuristic line/regex scanner (comments and string literals are stripped first), not a
full AST — adequate for a call graph with confidence levels. Tree-sitter-bsl can replace
it later for higher precision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_IDENT = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
_DECL_RE = re.compile(
    rf"^\s*(Процедура|Функция|Procedure|Function)\s+({_IDENT})\s*\((.*)$",
    re.IGNORECASE,
)
_END_RE = re.compile(r"^\s*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.IGNORECASE)
_CALL_RE = re.compile(rf"(?:({_IDENT})\s*\.\s*)?({_IDENT})\s*\(")
_DECL_MAX_LINES = 30  # сколько строк сигнатуры досматриваем в поисках `)` и `Экспорт`
# &Directive, optionally with a quoted argument: &Вместо("БазовыйМетод").
_DIRECTIVE_RE = re.compile(r'^\s*&([A-Za-zА-Яа-яЁё]+)\s*(?:\(\s*"([^"]*)"\s*\))?')

# Extension override annotations (their argument is the borrowed base method they hook). The
# rest (&НаКлиенте/&НаСервере/…) are compilation-context directives kept in Routine.directive.
_OVERRIDE_MODES = {
    "вместо": "Вместо", "перед": "Перед", "после": "После", "изменениеиконтроль": "ИзменениеИКонтроль",
    "around": "Вместо", "before": "Перед", "after": "После", "changeandvalidate": "ИзменениеИКонтроль",
}
_REGION_RE = re.compile(rf"^\s*#(?:Область|Region)\s+({_IDENT})", re.IGNORECASE)
_REGION_END_RE = re.compile(r"^\s*#(?:КонецОбласти|EndRegion)\b", re.IGNORECASE)

# Keywords that look like calls (`Keyword(`) but are not routine invocations.
_KEYWORDS = {
    w.lower()
    for w in (
        "Если", "Тогда", "ИначеЕсли", "Иначе", "КонецЕсли", "Для", "Каждого", "Из", "По",
        "Цикл", "КонецЦикла", "Пока", "Возврат", "Новый", "Прервать", "Продолжить",
        "Попытка", "Исключение", "КонецПопытки", "И", "Или", "Не", "Истина", "Ложь",
        "Неопределено", "Перейти", "Выполнить", "ВызватьИсключение", "Процедура", "Функция",
        "If", "Then", "ElsIf", "Else", "EndIf", "For", "Each", "In", "To", "Do", "While",
        "EndDo", "Return", "New", "Break", "Continue", "Try", "Except", "EndTry", "And",
        "Or", "Not", "True", "False", "Undefined", "Goto", "Execute", "Raise",
    )
}


@dataclass
class Call:
    qualifier: str | None
    method: str
    line: int = 0  # строка ПЕРВОГО вхождения вызова в модуле (1-based; 0 = неизвестно)


@dataclass
class Routine:
    name: str
    kind: str  # Procedure | Function
    export: bool
    start_line: int
    end_line: int
    region: str | None = None
    directive: str | None = None  # &НаКлиенте / &НаСервере / ... (compilation context)
    # Extension override annotation: mode (Вместо/Перед/После/ИзменениеИКонтроль) + the borrowed
    # base method it hooks (e.g. &Вместо("ПередЗаписью") -> ("Вместо", "ПередЗаписью")).
    override_mode: str | None = None
    override_target: str | None = None
    # Файл кончился без КонецПроцедуры/КонецФункции: рутина восстановлена по концу файла.
    unterminated: bool = False
    calls: list[Call] = field(default_factory=list)


def strip_comments_strings(text: str) -> str:
    """Replace string literals and // comments with spaces (preserving newlines)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':  # escaped quote inside string
                    out.append("  ")
                    i += 2
                    continue
                in_str = False
                out.append(" ")
            else:
                out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(" ")
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _find_calls(body: str, body_start_line: int = 0) -> list[Call]:
    """Вызовы из тела рутины. body_start_line — номер первой строки тела (1-based), чтобы у
    вызова была абсолютная координата в файле: без неё потребитель (find_callers) может дать
    только диапазон охватывающей рутины, и агенту приходится дочитывать её целиком."""
    calls: list[Call] = []
    for m in _CALL_RE.finditer(body):
        qualifier, method = m.group(1), m.group(2)
        if method.lower() in _KEYWORDS:
            continue
        if qualifier is not None and qualifier.lower() in _KEYWORDS:
            qualifier = None
        line = body_start_line + body.count("\n", 0, m.start()) if body_start_line else 0
        # Возвращаем КАЖДОЕ вхождение, а не по одному на пару (квалификатор, метод). Прежняя
        # дедупликация делала невидимыми повторные вызовы того же метода внутри одной рутины —
        # например второй `Модуль.Метод()` в ветке `Иначе`: на боевом методе так пропадало
        # 6 мест вызова из 254, а ответ при этом не был помечен неполным. Для вопроса «что
        # вызывает эта рутина» дедупликация делается на выдаче (find_callees), а не в данных.
        calls.append(Call(qualifier=qualifier, method=method, line=line))
    return calls


def parse_module(text: str) -> list[Routine]:
    clean = strip_comments_strings(text)
    lines = clean.split("\n")
    # Directives are matched on the RAW line: strip_comments_strings blanks string literals, which
    # would erase an override annotation's argument (&Вместо("Метод")). Indices stay aligned because
    # the cleaner preserves newlines. Declarations/calls still use the cleaned text.
    raw_lines = text.split("\n")
    routines: list[Routine] = []
    current: Routine | None = None
    body_start = 0
    region: str | None = None
    pending_directive: str | None = None
    pending_override: tuple[str, str | None] | None = None

    for idx, line in enumerate(lines):
        if current is None:
            dm = _DIRECTIVE_RE.match(raw_lines[idx])
            if dm:
                mode = _OVERRIDE_MODES.get(dm.group(1).lower())
                if mode:  # override annotation (keeps any compilation directive seen alongside)
                    pending_override = (mode, dm.group(2))
                else:
                    pending_directive = dm.group(1)
                continue
            rm = _REGION_RE.match(line)
            if rm:
                region = rm.group(1)
                continue
            if _REGION_END_RE.match(line):
                region = None
                continue
            decl = _DECL_RE.match(line)
            if decl:
                keyword, name = decl.group(1), decl.group(2)
                kind = "Function" if keyword.lower() in ("функция", "function") else "Procedure"
                # `Экспорт` стоит после ЗАКРЫВАЮЩЕЙ скобки, а список параметров в 1С часто
                # переносят на несколько строк — поиск только по строке объявления помечал
                # такие рутины неэкспортными (на УТ так врал каждый 25-й экспортный метод).
                decl_text, depth = line, line.count("(") - line.count(")")
                scan = idx
                while depth > 0 and scan + 1 < len(lines) and scan - idx < _DECL_MAX_LINES:
                    scan += 1
                    decl_text += " " + lines[scan]
                    depth += lines[scan].count("(") - lines[scan].count(")")
                export = bool(re.search(r"\)\s*(Экспорт|Export)\b", decl_text, re.IGNORECASE))
                current = Routine(name=name, kind=kind, export=export, start_line=idx + 1,
                                  end_line=idx + 1, region=region, directive=pending_directive,
                                  override_mode=pending_override[0] if pending_override else None,
                                  override_target=pending_override[1] if pending_override else None)
                pending_directive = None
                pending_override = None
                body_start = scan + 1  # тело начинается после всей (возможно многострочной) сигнатуры
        else:
            if _END_RE.match(line):
                current.end_line = idx + 1
                body = "\n".join(lines[body_start : idx])
                current.calls = _find_calls(body, body_start + 1)  # body_start — 0-based индекс
                routines.append(current)
                current = None
    if current is not None:
        # Файл кончился без КонецПроцедуры/КонецФункции (незакрытая последняя рутина — в УТ
        # таких файлов 16). Раньше рутина просто не попадала в результат ВМЕСТЕ СО ВСЕМИ своими
        # вызовами: она была невидима для поиска, индекса и ревью. Восстанавливаем по концу
        # файла и помечаем unterminated, чтобы потребитель мог отличить её от корректной.
        current.end_line = len(lines)
        current.calls = _find_calls("\n".join(lines[body_start:]), body_start + 1)
        current.unterminated = True
        routines.append(current)
    return routines
