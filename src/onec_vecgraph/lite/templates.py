"""Разбор макетов объектов 1С: структура вместо сырой разметки.

Зачем отдельный слой, а не `read_file`: макет схемы компоновки (СКД) на живой УТ — это 44 тыс.
символов XML, из которых половина приходится на текст запроса, а остальное на разметку настроек.
Модель почти всегда хочет знать НАБОРЫ ДАННЫХ, ПОЛЯ и ЗАПРОС — их и отдаём, а разметку опускаем.
Для прочих видов макетов (табличный документ, двоичные данные, картинка) разбирать нечего:
возвращаем описание и путь для read_file, честно об этом сказав.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

#: Пространство имён схемы компоновки данных (проверено на выгрузках 8.3).
_DCS_NS = "http://v8.1c.ru/8.1/data-composition-system/schema"


def _text(el: etree._Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _local(el: etree._Element) -> str:
    return etree.QName(el).localname


def parse_dcs(path: Path, *, max_query_chars: int = 8000,
              include_query: bool = True) -> dict:
    """Разобрать .dcs: наборы данных с полями, параметры, варианты настроек, текст запроса."""
    try:
        root = etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError) as exc:
        return {"error": f"Не удалось разобрать макет СКД: {exc}"}
    if etree.QName(root).namespace != _DCS_NS:
        return {"error": f"Это не схема компоновки данных (корень {_local(root)})."}

    data_sets: list[dict] = []
    query_total = 0
    for ds in root.findall(f"{{{_DCS_NS}}}dataSet"):
        fields = []
        for f in ds.findall(f"{{{_DCS_NS}}}field"):
            entry = {
                "field": _text(f.find(f"{{{_DCS_NS}}}dataPath")) or _text(
                    f.find(f"{{{_DCS_NS}}}field")),
                "title": _text(f.find(f"{{{_DCS_NS}}}title")),
            }
            if entry["field"] or entry["title"]:
                fields.append({k: v for k, v in entry.items() if v})
        query = _text(ds.find(f"{{{_DCS_NS}}}query"))
        query_total += len(query)
        row: dict = {
            "name": _text(ds.find(f"{{{_DCS_NS}}}name")),
            "type": ds.get("{http://www.w3.org/2001/XMLSchema-instance}type", ""),
            "field_count": len(fields),
            "fields": fields[:80],
            "fields_truncated": len(fields) > 80,
        }
        if include_query and query:
            row["query_chars"] = len(query)
            row["query"] = query[:max_query_chars]
            row["query_truncated"] = len(query) > max_query_chars
        elif query:
            row["query_chars"] = len(query)
            row["query_omitted"] = "include_query=True вернёт текст запроса"
        data_sets.append(row)

    parameters = []
    for p in root.findall(f"{{{_DCS_NS}}}parameter"):
        entry = {
            "name": _text(p.find(f"{{{_DCS_NS}}}name")),
            "title": _text(p.find(f"{{{_DCS_NS}}}title")),
            "type": _text(p.find(f"{{{_DCS_NS}}}valueType")),
        }
        parameters.append({k: v for k, v in entry.items() if v})

    variants = [_text(v.find(f"{{{_DCS_NS}}}name"))
                for v in root.findall(f"{{{_DCS_NS}}}settingsVariant")]
    return {
        "template_type": "DataCompositionSchema",
        "data_sets": data_sets,
        "parameters": parameters,
        "settings_variants": [v for v in variants if v],
        "query_chars_total": query_total,
    }
