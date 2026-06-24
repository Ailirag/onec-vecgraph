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


def _find_calls(body: str) -> list[Call]:
    calls: list[Call] = []
    seen: set[tuple[str | None, str]] = set()
    for m in _CALL_RE.finditer(body):
        qualifier, method = m.group(1), m.group(2)
        if method.lower() in _KEYWORDS:
            continue
        if qualifier is not None and qualifier.lower() in _KEYWORDS:
            qualifier = None
        key = (qualifier, method)
        if key in seen:
            continue
        seen.add(key)
        calls.append(Call(qualifier=qualifier, method=method))
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
                export = bool(re.search(r"\)\s*(Экспорт|Export)\b", line, re.IGNORECASE))
                current = Routine(name=name, kind=kind, export=export, start_line=idx + 1,
                                  end_line=idx + 1, region=region, directive=pending_directive,
                                  override_mode=pending_override[0] if pending_override else None,
                                  override_target=pending_override[1] if pending_override else None)
                pending_directive = None
                pending_override = None
                body_start = idx + 1
        else:
            if _END_RE.match(line):
                current.end_line = idx + 1
                body = "\n".join(lines[body_start : idx])
                current.calls = _find_calls(body)
                routines.append(current)
                current = None
    return routines
