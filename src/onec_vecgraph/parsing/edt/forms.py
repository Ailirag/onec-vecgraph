"""Read an EDT managed form (.form) — event handlers and human-readable captions.

EDT serializes a form as `<form:Form>` (ns `http://g5.1c.ru/v8/dt/form`); its child elements are
in no namespace. Events live in `<handlers><event>OnChange</event><name>Proc</name></handlers>`
blocks; captions in `<title>/<toolTip>` blocks with `<key>/<value>` localized text.
"""

from __future__ import annotations

from lxml import etree


def parse_form_handlers(path: str) -> list[dict]:
    """Map EDT form events to handler procedure names: [{event, handler, element}]."""
    try:
        root = etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return []
    out: list[dict] = []
    for h in root.iter("handlers"):
        event = (h.findtext("event") or "").strip()
        handler = (h.findtext("name") or "").strip()
        if not event or not handler:
            continue
        parent = h.getparent()
        nm = parent.find("name") if parent is not None else None
        element = nm.text.strip() if nm is not None and nm.text else None
        out.append({"event": event, "handler": handler, "element": element})
    return out


def extract_form_text(path: str, max_items: int = 60, max_len: int = 2000) -> str:
    """Collect human-readable form captions (form/item <title> and <toolTip> values)."""
    try:
        root = etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return ""
    seen: set[str] = set()
    parts: list[str] = []
    for caption_tag in ("title", "toolTip"):
        for el in root.iter(caption_tag):
            value = el.find("value")
            text = (value.text or "").strip() if value is not None else ""
            if text and text not in seen:
                seen.add(text)
                parts.append(text)
                if len(parts) >= max_items:
                    return " · ".join(parts)[:max_len]
    return " · ".join(parts)[:max_len]
