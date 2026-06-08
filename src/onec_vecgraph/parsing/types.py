"""Parse 1C type descriptions (<Type> / <Source> blocks).

A type description contains one or more <v8:Type> entries (plus optional
<v8:TypeSet>). Entries are QNames such as:
  - xs:string, xs:decimal, xs:boolean, xs:dateTime  -> primitive
  - v8:UUID, v8:ValueStorage                         -> platform value type
  - cfg:CatalogRef.AI_Провайдеры                     -> reference to Catalog.AI_Провайдеры
  - cfg:CatalogObject.Контрагенты                    -> reference to Catalog.Контрагенты
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from .ns import CFG, V8, q

# Category suffixes of generated 1C types, longest first so e.g. "RecordKey"
# is matched before "Record".
_CATEGORY = (
    "RecordKey",
    "RecordManager",
    "RecordSet",
    "TabularSectionRow",
    "TabularSection",
    "Selection",
    "Manager",
    "Object",
    "Record",
    "List",
    "Ref",
)
_REF_RE = re.compile(r"^(?P<root>.+?)(?P<cat>" + "|".join(_CATEGORY) + r")\.(?P<name>.+)$")

# Type-class stem -> metadata object kind, where they differ. Constants are referenced
# via ConstantValueManager/ConstantValueKey, but the object kind is 'Constant'.
_REF_KIND_ALIASES = {"ConstantValue": "Constant"}


@dataclass
class TypeRef:
    raw: str
    category: str  # 'primitive' | 'reference' | 'typeset'
    ref_kind: str | None = None  # e.g. 'Catalog'
    ref_name: str | None = None  # e.g. 'AI_Провайдеры'

    @property
    def ref_fqn(self) -> str | None:
        if self.ref_kind and self.ref_name:
            return f"{self.ref_kind}.{self.ref_name}"
        return None


@dataclass
class TypeDescription:
    refs: list[TypeRef] = field(default_factory=list)

    @property
    def is_composite(self) -> bool:
        return len(self.refs) > 1

    @property
    def references(self) -> list[TypeRef]:
        return [r for r in self.refs if r.category == "reference"]

    def render(self) -> str:
        return ", ".join(r.raw for r in self.refs)


def _resolve(value: str, nsmap: dict) -> TypeRef:
    value = value.strip()
    prefix, _, local = value.partition(":")
    if not local:  # no prefix
        return TypeRef(raw=value, category="primitive")
    ns = nsmap.get(prefix)
    if ns == CFG:
        m = _REF_RE.match(local)
        if m:
            root = _REF_KIND_ALIASES.get(m.group("root"), m.group("root"))
            return TypeRef(value, "reference", root, m.group("name"))
        root, _, name = local.partition(".")
        if name:
            return TypeRef(value, "reference", _REF_KIND_ALIASES.get(root, root), name)
        return TypeRef(value, "reference", None, None)
    # xs:* and v8:* are primitive / platform value types
    return TypeRef(raw=value, category="primitive")


def parse_type(block: etree._Element | None) -> TypeDescription:
    """Parse a <Type> or <Source> element into a TypeDescription."""
    desc = TypeDescription()
    if block is None:
        return desc
    nsmap = {k: v for k, v in block.nsmap.items() if k}
    for entry in block.findall(q(V8, "Type")):
        if entry.text:
            desc.refs.append(_resolve(entry.text, entry.nsmap or nsmap))
    for entry in block.findall(q(V8, "TypeSet")):
        if entry.text:
            ref = _resolve(entry.text, entry.nsmap or nsmap)
            ref.category = "typeset"
            desc.refs.append(ref)
    return desc
