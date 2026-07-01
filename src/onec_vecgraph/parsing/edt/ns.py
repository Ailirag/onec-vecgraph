"""EDT .mdo helpers.

In an .mdo file only the ROOT element is namespaced (`mdclass:Catalog`, `mcore` for some
value types); all the child elements we read (`name`, `synonym`, `attributes`, `type`/`types`,
`tabularSections`, `enumValues`, `content`, `registerRecords`, `forms`, `objectBelonging`, …)
are in NO namespace. So child access is plain local-name find/findall — no QName juggling.
"""

from __future__ import annotations

from lxml import etree

# EDT metadata namespace (root element only).
MDCLASS = "http://g5.1c.ru/v8/dt/metadata/mdclass"
MCORE = "http://g5.1c.ru/v8/dt/mcore"


def local_name(tag: object) -> str:
    """Local name of an element tag (drops the namespace)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def child_text(parent: etree._Element | None, name: str, default: str = "") -> str:
    """Text of the first no-namespace child <name>, stripped."""
    if parent is None:
        return default
    el = parent.find(name)
    if el is None or el.text is None:
        return default
    return el.text.strip()


def child_texts(parent: etree._Element | None, name: str) -> list[str]:
    """Text of every no-namespace child <name> (non-empty, stripped)."""
    if parent is None:
        return []
    out: list[str] = []
    for el in parent.findall(name):
        if el.text and el.text.strip():
            out.append(el.text.strip())
    return out


def synonym(el: etree._Element | None, lang: str = "ru") -> str:
    """Read EDT <synonym><key>ru</key><value>…</value></synonym> (one block per language)."""
    if el is None:
        return ""
    fallback = ""
    for syn in el.findall("synonym"):
        value = child_text(syn, "value")
        if not value:
            continue
        if child_text(syn, "key") == lang:
            return value
        fallback = fallback or value
    return fallback
