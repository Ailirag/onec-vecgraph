"""Build text chunks (cards) from metadata for embedding.

Each chunk carries:
  - text:        the rich, meaning-oriented card (synonym + comment + types) with a
                 context prefix ("breadcrumbs") so it is self-contained;
  - text_ident:  a short identifier-oriented representation (technical name + context).

The two representations feed a multi-vector index (name × meaning), fused with the
full-text index via RRF at query time. Chunks always link back to their top-level Object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Identifier splitting for full-text: 1C identifiers are CamelCase / ВерхнийРегистр and
# dotted (ОбщийМодуль.Метод). The standard FTS analyzer does NOT split sub-words, so a
# query like "Продажи" would miss "ПродажиТоваров". We pre-split identifiers into tokens
# and index them alongside the human text (and tokenize queries the same way).
_NONWORD = re.compile(r"[^0-9A-Za-zА-Яа-яЁё]+")
_CAMEL = re.compile(r"(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])")


def search_tokens(*sources: str | None) -> str:
    """Split identifiers into sub-word tokens (ПродажиТоваров → Продажи Товаров), keeping the
    originals; case-insensitively de-duplicated, order preserved. Shared by indexing + query."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        low = tok.lower()
        if low and low not in seen:
            seen.add(low)
            out.append(tok)

    for src in sources:
        if not src:
            continue
        for part in _NONWORD.split(src):
            if not part:
                continue
            _add(part)
            for sub in _CAMEL.sub(" ", part).split():
                _add(sub)
    return " ".join(out)

KIND_RU = {
    "Catalog": "Справочник",
    "Document": "Документ",
    "Enum": "Перечисление",
    "InformationRegister": "Регистр сведений",
    "AccumulationRegister": "Регистр накопления",
    "AccountingRegister": "Регистр бухгалтерии",
    "CalculationRegister": "Регистр расчёта",
    "ChartOfCharacteristicTypes": "План видов характеристик",
    "ChartOfAccounts": "План счетов",
    "ChartOfCalculationTypes": "План видов расчёта",
    "CommonModule": "Общий модуль",
    "Report": "Отчёт",
    "DataProcessor": "Обработка",
    "Constant": "Константа",
    "ExchangePlan": "План обмена",
    "BusinessProcess": "Бизнес-процесс",
    "Task": "Задача",
    "Subsystem": "Подсистема",
    "Role": "Роль",
    "DocumentJournal": "Журнал документов",
    "DefinedType": "Определяемый тип",
    "EventSubscription": "Подписка на событие",
}

_MAX_ATTRS_IN_CARD = 30
_MAX_MEMBERS_IN_CARD = 40  # subsystem composition / role rights listed in a chunk
_ROLE_RU = {"Attribute": "реквизит", "Dimension": "измерение", "Resource": "ресурс"}

# Standard 1C object/manager/recordset module event handlers → a coarse "what triggers this"
# category. Lets testers/reviewers find behavior entry points (proving, validation, write…).
# Keys are lower-cased routine names (RU configs; a few EN aliases for safety).
OBJECT_EVENT_HANDLERS = {
    "обработкапроведения": "проведение",
    "обработкаудаленияпроведения": "проведение",
    "posting": "проведение",
    "передзаписью": "запись",
    "призаписи": "запись",
    "beforewrite": "запись",
    "onwrite": "запись",
    "передудалением": "удаление",
    "beforedelete": "удаление",
    "обработказаполнения": "заполнение",
    "filling": "заполнение",
    "прикопировании": "заполнение",
    "обработкапроверкизаполнения": "проверка_заполнения",
    "проверказаполнения": "проверка_заполнения",
    "fillcheckprocessing": "проверка_заполнения",
    "приустановкеновогономера": "нумерация",
    "приустановкеновогокода": "нумерация",
    "обработкаполученияданныхвыбора": "выбор",
    "приполученииданныхвыбора": "выбор",
}


def kind_ru(kind: str) -> str:
    return KIND_RU.get(kind, kind)


def classify_entry_point(name: str, form_event: str | None = None) -> str | None:
    """Coarse behavior-trigger category for a routine, or None if it isn't a known entry point.
    Form-wired handlers are 'событие_формы'; standard module events map via OBJECT_EVENT_HANDLERS."""
    if form_event:
        return "событие_формы"
    return OBJECT_EVENT_HANDLERS.get(name.lower())


@dataclass
class Chunk:
    fqn: str  # unique chunk id
    owner_fqn: str  # top-level Object this chunk attaches to
    chunk_kind: str  # object | attribute | tabular_attribute | enum_value | predefined
    name: str
    synonym: str
    text: str  # semantic representation (embedded + full-text indexed)
    text_ident: str  # identifier representation (embedded only)
    config_id: str
    config_version: str | None = None  # owner object's configVersion at embed time
    entry_point: str | None = None  # behavior-trigger category for code chunks (see classify_entry_point)
    source: str = "config"  # corpus: config | its | artifact (multi-source vectorization)

    def props(self) -> dict[str, Any]:
        return {
            "fqn": self.fqn,
            "owner_fqn": self.owner_fqn,
            "chunk_kind": self.chunk_kind,
            "name": self.name,
            "synonym": self.synonym,
            "text": self.text,
            "text_ident": self.text_ident,
            # Split-identifier tokens, full-text indexed alongside `text` so sub-word and
            # CamelCase queries match (see search_tokens()).
            "text_tokens": search_tokens(self.text_ident, self.name),
            "entry_point": self.entry_point,
            "source": self.source,
            "config_id": self.config_id,
            "config_version": self.config_version,
        }


def _clean(s: str | None) -> str:
    return (s or "").strip()


def object_chunk(o: dict[str, Any]) -> Chunk:
    kind, name, syn = o["kind"], o["name"], _clean(o.get("synonym"))
    parts = [f"{kind_ru(kind)} «{syn or name}» ({name})"]
    if _clean(o.get("comment")):
        parts.append(_clean(o["comment"]))
    fields = [f for f in o.get("fields", []) if f and f.get("name")]
    if fields:
        rendered = "; ".join(
            f"{_clean(f.get('syn')) or f['name']}: {_clean(f.get('type')) or '—'}"
            for f in fields[:_MAX_ATTRS_IN_CARD]
        )
        label = "Реквизиты" if kind in ("Catalog", "Document") else "Поля"
        parts.append(f"{label}: {rendered}")
    return Chunk(
        fqn=f"{o['fqn']}#object",
        owner_fqn=o["fqn"],
        chunk_kind="object",
        name=name,
        synonym=syn,
        text=". ".join(parts),
        text_ident=f"{name} {syn}".strip(),
        config_id=o.get("config_id", ""),
        config_version=o.get("config_version"),
    )


def attribute_chunks(o: dict[str, Any]) -> list[Chunk]:
    owner_kind, owner_syn, owner_name = o["kind"], _clean(o.get("synonym")), o["name"]
    prefix = f"{kind_ru(owner_kind)} «{owner_syn or owner_name}»"
    out: list[Chunk] = []
    for f in o.get("fields", []):
        if not f or not f.get("name") or not f.get("fqn"):
            continue
        syn = _clean(f.get("syn")) or f["name"]
        role = _ROLE_RU.get(f.get("role", "Attribute"), "реквизит")
        text = f"{prefix} ▸ {role} «{syn}» ({f['name']}): {_clean(f.get('type')) or '—'}"
        if _clean(f.get("comment")):
            text += f". {_clean(f['comment'])}"
        out.append(
            Chunk(
                fqn=f"{f['fqn']}#attr",
                owner_fqn=o["fqn"],
                chunk_kind="attribute",
                name=f["name"],
                synonym=syn,
                text=text,
                text_ident=f"{f['name']} {owner_name}",
                config_id=o.get("config_id", ""),
                config_version=o.get("config_version"),
            )
        )
    return out


def tabular_attribute_chunk(row: dict[str, Any]) -> Chunk:
    owner_syn = _clean(row.get("owner_syn")) or row["owner_name"]
    ts_syn = _clean(row.get("ts_syn")) or row["ts_name"]
    syn = _clean(row.get("field_syn")) or row["field_name"]
    text = (
        f"{kind_ru(row['owner_kind'])} «{owner_syn}» ▸ табличная часть «{ts_syn}» ▸ "
        f"реквизит «{syn}» ({row['field_name']}): {_clean(row.get('type')) or '—'}"
    )
    return Chunk(
        fqn=f"{row['field_fqn']}#tsattr",
        owner_fqn=row["owner_fqn"],
        chunk_kind="tabular_attribute",
        name=row["field_name"],
        synonym=syn,
        text=text,
        text_ident=f"{row['field_name']} {row['ts_name']} {row['owner_name']}",
        config_id=row.get("config_id", ""),
        config_version=row.get("config_version"),
    )


_MIN_CODE_NONWS = 80  # skip near-empty / boilerplate routines below this many non-ws chars
_CODE_BUDGET_NONWS = 1200  # per-chunk budget (non-ws chars); larger routines split into parts


def _split_code(raw_text: str, budget: int) -> list[str]:
    """cAST-style split-then-merge over a routine body: greedily pack whole lines up to a
    non-whitespace-char budget, preferring to break on blank lines. Never truncates."""
    lines = raw_text.split("\n")
    segments: list[str] = []
    cur: list[str] = []
    cur_nonws = 0
    for line in lines:
        w = len("".join(line.split()))
        # Start a new segment when over budget; prefer the boundary at a blank line.
        if cur and cur_nonws + w > budget and (not line.strip() or cur_nonws >= budget):
            segments.append("\n".join(cur))
            cur, cur_nonws = [], 0
        cur.append(line)
        cur_nonws += w
    if cur:
        segments.append("\n".join(cur))
    return segments or [raw_text]


def code_chunks(routine: Any, raw_text: str, ctx: dict[str, Any]) -> list[Chunk]:
    """Per-routine code chunk(s) with a context prefix (object ▸ [form] ▸ directive ▸ name
    [▸ event]). Large routines split into budgeted parts; entry-point routines are kept even
    if short and tagged with a behavior category."""
    handler = ctx.get("handlers", {}).get(routine.name)
    entry_point = classify_entry_point(routine.name, form_event=handler["event"] if handler else None)
    if len("".join(raw_text.split())) < _MIN_CODE_NONWS and not entry_point:
        return []  # boilerplate / stub (but always keep behavior entry points)

    owner_syn = _clean(ctx.get("owner_syn")) or ctx["owner_name"]
    head = f"{kind_ru(ctx['owner_kind'])} «{owner_syn}»"
    if ctx.get("form_name"):
        head += f" ▸ форма «{ctx['form_name']}»"
    elif ctx.get("module_type"):
        head += f" ▸ {ctx['module_type']}"
    directive = f"{routine.directive} " if getattr(routine, "directive", None) else ""
    sig = f"{directive}{routine.kind} {routine.name}"
    if handler:
        sig += f" (обработчик {handler['event']} элемента {handler.get('element') or 'формы'})"
    if entry_point:
        sig += f" [точка входа: {entry_point}]"

    ctx_label = ctx.get("form_name") or ctx.get("module_type") or ""
    text_ident = f"{routine.name} {ctx_label} {ctx['owner_name']}".strip()
    segments = _split_code(raw_text, _CODE_BUDGET_NONWS)
    multi = len(segments) > 1
    out: list[Chunk] = []
    for i, seg in enumerate(segments):
        suffix = f"/{i}" if multi else ""
        part = f" (часть {i + 1}/{len(segments)})" if multi else ""
        out.append(
            Chunk(
                fqn=f"{ctx['module_fqn']}::{routine.name}#code{suffix}",
                owner_fqn=ctx["owner_fqn"],
                chunk_kind="code",
                name=routine.name,
                synonym=routine.name,
                text=f"{head} ▸ {sig}{part}\n{seg}",
                text_ident=text_ident,
                config_id=ctx.get("config_id", ""),
                config_version=ctx.get("config_version"),
                entry_point=entry_point,
            )
        )
    return out


def form_chunk(row: dict[str, Any]) -> Chunk:
    owner_syn = _clean(row.get("owner_syn")) or row["owner_name"]
    text = f"{kind_ru(row['owner_kind'])} «{owner_syn}» ▸ форма «{row['form_name']}»"
    ftext = _clean(row.get("form_text"))
    if ftext:
        text += f". {ftext}"
    return Chunk(
        fqn=f"{row['form_fqn']}#form",
        owner_fqn=row["owner_fqn"],
        chunk_kind="form",
        name=row["form_name"],
        synonym=row["form_name"],
        text=text,
        text_ident=f"{row['form_name']} {row['owner_name']}",
        config_id=row.get("config_id", ""),
        config_version=row.get("config_version"),
    )


def enum_value_chunk(row: dict[str, Any]) -> Chunk:
    syn = _clean(row.get("value_syn")) or row["value_name"]
    enum_syn = _clean(row.get("enum_syn")) or row["enum_name"]
    text = f"Перечисление «{enum_syn}» ▸ значение «{syn}» ({row['value_name']})"
    return Chunk(
        fqn=f"{row['value_fqn']}#val",
        owner_fqn=row["enum_fqn"],
        chunk_kind="enum_value",
        name=row["value_name"],
        synonym=syn,
        text=text,
        text_ident=f"{row['value_name']} {row['enum_name']}",
        config_id=row.get("config_id", ""),
        config_version=row.get("config_version"),
    )


def predefined_chunk(row: dict[str, Any]) -> Chunk:
    desc = _clean(row.get("descr")) or row["pre_name"]
    owner_syn = _clean(row.get("owner_syn")) or row["owner_name"]
    text = (
        f"{kind_ru(row['owner_kind'])} «{owner_syn}» ▸ "
        f"предопределённый элемент «{desc}» ({row['pre_name']})"
    )
    return Chunk(
        fqn=f"{row['pre_fqn']}#pre",
        owner_fqn=row["owner_fqn"],
        chunk_kind="predefined",
        name=row["pre_name"],
        synonym=desc,
        text=text,
        text_ident=f"{row['pre_name']} {row['owner_name']}",
        config_id=row.get("config_id", ""),
        config_version=row.get("config_version"),
    )


def subsystem_chunk(row: dict[str, Any]) -> Chunk:
    """A subsystem as a first-class semantic unit: business name + comment + its composition,
    so a functional/business query can discover the right area of the configuration."""
    syn = _clean(row.get("synonym")) or row["name"]
    parts = [f"Подсистема «{syn}» ({row['name']})"]
    if _clean(row.get("comment")):
        parts.append(_clean(row["comment"]))
    members = [m for m in row.get("members", []) if m and m.get("name")]
    if members:
        rendered = "; ".join(_clean(m.get("syn")) or m["name"] for m in members[:_MAX_MEMBERS_IN_CARD])
        parts.append(f"Состав: {rendered}")
    return Chunk(
        fqn=f"{row['fqn']}#subsystem",
        owner_fqn=row["fqn"],
        chunk_kind="subsystem",
        name=row["name"],
        synonym=syn,
        text=". ".join(parts),
        text_ident=f"{row['name']} {syn}".strip(),
        config_id=row.get("config_id", ""),
        config_version=row.get("config_version"),
    )


def role_chunk(row: dict[str, Any]) -> Chunk:
    """A role as a first-class semantic unit: which objects it grants which rights on, so an
    access/permissions question is answerable by search (not only via Cypher)."""
    syn = _clean(row.get("synonym")) or row["name"]
    parts = [f"Роль «{syn}» ({row['name']})"]
    if _clean(row.get("comment")):
        parts.append(_clean(row["comment"]))
    rights = [r for r in row.get("rights", []) if r and r.get("name")]
    if rights:
        rendered = "; ".join(
            f"{_clean(r.get('syn')) or r['name']}: {', '.join(r.get('granted') or [])}"
            for r in rights[:_MAX_MEMBERS_IN_CARD]
        )
        parts.append(f"Права на объекты: {rendered}")
    return Chunk(
        fqn=f"{row['fqn']}#role",
        owner_fqn=row["fqn"],
        chunk_kind="role",
        name=row["name"],
        synonym=syn,
        text=". ".join(parts),
        text_ident=f"{row['name']} {syn}".strip(),
        config_id=row.get("config_id", ""),
        config_version=row.get("config_version"),
    )


def doc_chunks(title: str, text: str, *, source: str, owner_fqn: str, config_id: str = "",
               section_path: list[str] | None = None) -> list["Chunk"]:
    """Chunk an external document section (ITS / artifact) into one or more :Chunk under a doc owner.
    Breadcrumb prefix = title ▸ section_path; large sections split by the same budget as code.
    chunk_kind == source (its | artifact)."""
    section_path = section_path or []
    head = " ▸ ".join([title, *section_path]) if section_path else title
    segments = _split_code(text, _CODE_BUDGET_NONWS)
    multi = len(segments) > 1
    ident = f"{title} {' '.join(section_path)}".strip()
    out: list[Chunk] = []
    for i, seg in enumerate(segments):
        suffix = f"#chunk/{i}" if multi else "#chunk"
        part = f" (часть {i + 1}/{len(segments)})" if multi else ""
        out.append(
            Chunk(
                fqn=f"{owner_fqn}{suffix}",
                owner_fqn=owner_fqn,
                chunk_kind=source,
                name=title,
                synonym=title,
                text=f"{head}{part}\n{seg}",
                text_ident=ident,
                config_id=config_id,
                config_version=None,
                source=source,
            )
        )
    return out
