"""XML namespaces of the 1C Configurator dump and small lxml helpers."""

from __future__ import annotations

from lxml import etree

# Namespace URIs used across the Configurator XML dump.
MD = "http://v8.1c.ru/8.3/MDClasses"
V8 = "http://v8.1c.ru/8.1/data/core"
XR = "http://v8.1c.ru/8.3/xcf/readable"
CFG = "http://v8.1c.ru/8.1/data/enterprise/current-config"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ROLES = "http://v8.1c.ru/8.2/roles"
PREDEF = "http://v8.1c.ru/8.3/xcf/predef"
DUMPINFO = "http://v8.1c.ru/8.3/xcf/dumpinfo"
LOGFORM = "http://v8.1c.ru/8.3/xcf/logform"  # managed form (Ext/Form.xml)


def q(ns: str, local: str) -> str:
    """Clark-notation qualified tag, e.g. q(MD, 'Name') -> '{...}Name'."""
    return f"{{{ns}}}{local}"


def ln(tag: object) -> str:
    """Local name of an element tag (drops the namespace)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def first_child_element(el: etree._Element) -> etree._Element | None:
    for child in el:
        if isinstance(child.tag, str):  # skip comments / PIs
            return child
    return None


def text(parent: etree._Element | None, tag: str, default: str = "") -> str:
    if parent is None:
        return default
    child = parent.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def synonym(props: etree._Element | None, lang: str = "ru") -> str:
    """Read a <Synonym> multilingual value, preferring the given language."""
    if props is None:
        return ""
    syn = props.find(q(MD, "Synonym"))
    if syn is None:
        return ""
    fallback = ""
    for item in syn.findall(q(V8, "item")):
        content = item.find(q(V8, "content"))
        if content is None or content.text is None:
            continue
        value = content.text.strip()
        item_lang = item.find(q(V8, "lang"))
        if item_lang is not None and (item_lang.text or "").strip() == lang:
            return value
        fallback = fallback or value
    return fallback
