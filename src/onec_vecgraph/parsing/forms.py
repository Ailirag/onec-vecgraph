"""Extract searchable text (titles, captions, tooltips) from a managed-form Ext/Form.xml."""

from __future__ import annotations

from lxml import etree

from .ns import LOGFORM, V8, q


def parse_form_handlers(path: str) -> list[dict]:
    """Map managed-form events to handler procedure names: [{event, handler, element}].

    In Ext/Form.xml: <Event name="OnChange">ИмяОбработчика</Event> inside an item's <Events>.
    """
    try:
        root = etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return []
    out: list[dict] = []
    for ev in root.iter(q(LOGFORM, "Event")):
        handler = (ev.text or "").strip()
        if not handler:
            continue
        events_el = ev.getparent()
        item_el = events_el.getparent() if events_el is not None else None
        out.append({
            "event": ev.get("name"),
            "handler": handler,
            "element": item_el.get("name") if item_el is not None else None,
        })
    return out


def extract_form_text(path: str, max_items: int = 60, max_len: int = 2000) -> str:
    """Collect human-readable <v8:content> texts (form title + item captions) from a form."""
    try:
        root = etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return ""
    seen: set[str] = set()
    parts: list[str] = []
    for el in root.iter(q(V8, "content")):
        t = (el.text or "").strip()
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
            if len(parts) >= max_items:
                break
    return " · ".join(parts)[:max_len]
