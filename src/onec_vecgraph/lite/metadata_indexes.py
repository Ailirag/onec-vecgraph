"""Read declared database indexing, not the code-search/FTS index or live SQL schema.

Configurator: object.xml + Object/Ext/AdditionalIndexes.xml.
EDT: Object/Object.mdo + Object/AdditionalIndexes.aindex. No index rebuild needed.
Keep base and extension declarations separate: an adopted object is only a fragment.
"""
from __future__ import annotations

from pathlib import Path

from lxml import etree

from .metaview import _children, _prop
from ..parsing.ns import first_child_element, ln

RULES_VERSION = "8.3.27-20261102"
RULES_URL = "https://its.1c.ru/db/content/metod8dev/src/admins/i8101590.htm"
GUIDE_URL = "https://1c-dn.com/library/tutorials/1c_enterprise_developer_guide_8_3_27/"
FIELD_TAGS = {
    "attribute": "Attribute", "attributes": "Attribute",
    "dimension": "Dimension", "dimensions": "Dimension",
    "resource": "Resource", "resources": "Resource",
    "column": "Column", "columns": "Column",
    "addressingattribute": "AddressingAttribute", "addressingattributes": "AddressingAttribute",
    "standardattribute": "StandardAttribute", "standardattributes": "StandardAttribute",
}
SETTING_NAMES = {
    "indexing", "mainfilter", "useintotals", "leading", "denyincompletevalues",
    "master", "index", "fulltextsearch",
}
OBJECT_SETTINGS = {
    "codeLength", "descriptionLength", "numberLength", "defaultPresentation",
    "hierarchical", "hierarchyType", "placeFoldersOnTop", "codeSeries", "numberPeriodicity",
    "informationRegisterPeriodicity", "writeMode", "mainFilterOnPeriod",
    "enableTotalsSliceFirst", "enableTotalsSliceLast", "registerType", "useTotals",
    "useStandardCommands", "dataSeparation", "dataSeparationUse", "autoUse",
}
MODES = {"dontindex": "not_requested", "index": "index",
         "indexwithadditionalorder": "index_with_additional_order"}
STORAGE_KINDS = frozenset({
    "Catalog", "Document", "DocumentJournal", "ChartOfCharacteristicTypes",
    "ChartOfAccounts", "ChartOfCalculationTypes", "ExchangePlan", "BusinessProcess", "Task",
    "InformationRegister", "AccumulationRegister", "AccountingRegister", "CalculationRegister",
    "Sequence", "Enum", "Constant", "CommonAttribute",
})


def _read(path: Path) -> etree._Element:
    # A metadata file must never fetch network resources or expand external entities.
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.parse(str(path), parser).getroot()
    if ln(root.tag) == "MetaDataObject":
        root = first_child_element(root)
    if root is None:
        raise ValueError(f"Empty metadata: {path.name}")
    return root


def _props(el: etree._Element) -> list[etree._Element]:
    return [p for c in el if isinstance(c.tag, str)
            for p in (list(c) if ln(c.tag).lower() == "properties" else [c])
            if isinstance(p.tag, str)]


def _settings(el: etree._Element, wanted: set[str]) -> dict[str, str]:
    keys = {s.lower() for s in wanted}
    return {ln(p.tag): (p.text or "").strip() for p in _props(el)
            if ln(p.tag).lower() in keys}


def _fields(el: etree._Element, table: str, path: Path) -> list[dict]:
    rows = []
    for f in _children(el, *FIELD_TAGS):
        name = _prop(f, "name") or f.get("name", "")
        settings = _settings(f, SETTING_NAMES)
        mode = next((v for k, v in settings.items() if k.lower() == "indexing"), "")
        rows.append({"entry_type": "field", "table": table, "field": name,
                     "field_role": FIELD_TAGS[ln(f.tag).lower()],
                     "indexing": mode or None,
                     "declaration": "explicit" if mode else "unspecified",
                     "index_request": MODES.get(mode.lower(), "unknown" if mode else "unspecified"),
                     "settings": settings, "metadata_file": str(path), "line": f.sourceline})
    return rows


def _index_fields(el: etree._Element, tag: str) -> list[str]:
    result = []
    for container in _children(el, tag):
        # Configurator wraps Field items; EDT repeats indexedFields/path objects.
        for child in container:
            if isinstance(child.tag, str) and ln(child.tag).lower() in {"field", "path"}:
                result.append((child.text or "").strip())
    return result


def read_index_declarations(meta: Path, obj_dir: Path, kind: str, name: str) -> dict:
    root = _read(meta)
    fqn = f"{kind}.{name}"
    if ln(root.tag) != kind:
        raise ValueError(f"Expected {kind}, found {ln(root.tag)} in {meta.name}")
    entries = _fields(root, fqn, meta)
    if kind == "CommonAttribute":
        settings = _settings(root, SETTING_NAMES)
        mode = next((v for k, v in settings.items() if k.lower() == "indexing"), "")
        entries.append({"entry_type": "field", "table": "shared_attribute_usage",
                        "field": name, "field_role": kind, "indexing": mode or None,
                        "declaration": "explicit" if mode else "unspecified",
                        "index_request": MODES.get(mode.lower(), "unknown" if mode else "unspecified"),
                        "settings": settings, "metadata_file": str(meta), "line": root.sourceline})
    tables = []
    for section in _children(root, "TabularSection", "tabularSections"):
        table = f"{fqn}.{_prop(section, 'name')}"
        tables.append(table)
        entries.extend(_fields(section, table, meta))
    # Do not pick a random neighbouring file: only the documented sidecar for this object.
    sidecar = (obj_dir / "AdditionalIndexes.aindex" if meta.suffix == ".mdo"
               else obj_dir / "Ext" / "AdditionalIndexes.xml")
    additional_state = "not_present_in_dump"
    if sidecar.is_file():
        indexes = _read(sidecar)
        if ln(indexes.tag).lower() != "additionalindexes":
            raise ValueError(f"Invalid additional indexes file: {sidecar.name}")
        additional_state = "present"
        for idx in _children(indexes, "AdditionalIndex", "indexes"):
            if not _prop(idx, "name") or not _prop(idx, "table") or not _index_fields(idx, "IndexedFields"):
                raise ValueError(f"Incomplete additional index declaration: {sidecar.name}")
            entries.append({"entry_type": "additional_index", "name": _prop(idx, "name"),
                            "id": idx.get("id") or _prop(idx, "id"),
                            "table": _prop(idx, "table"),
                            "key_fields": _index_fields(idx, "IndexedFields"),
                            "included_fields": _index_fields(idx, "AdditionalFields"),
                            "metadata_file": str(sidecar), "line": idx.sourceline,
                            "physical_presence": "not_verified"})
    return {"object_settings": _settings(root, OBJECT_SETTINGS),
            "dimension_order": [_prop(x, "name") for x in _children(root, "Dimension", "dimensions")],
            "tabular_tables": tables, "additional_indexes_file": str(sidecar),
            "additional_indexes_state": additional_state, "entries": entries}


def indexing_rules(kind: str) -> dict:
    return {
        "version": RULES_VERSION, "references": [RULES_URL, GUIDE_URL],
        "storage_metadata_kind": kind in STORAGE_KINDS,
        "scope": "metadata_declarations_not_live_database",
        "automatic_indexes": "Platform indexes depend on object kind, standard attributes, dimensions, "
        "periodicity, recorder, hierarchy, owners, selection criteria and data separators. "
        "This tool does not enumerate their physical key columns; consult the linked platform rules.",
        "field_indexing": "DontIndex means no separate index requested by this property, not absence "
        "from all indexes. Missing properties are unspecified (EDT defaults/adopted fragments). "
        "FullTextSearch is a separate mechanism, not a database B-tree index.",
        "additional_indexes": "Available since 8.3.26 for supported storage objects, journals, sequences "
        "and registers. Ordered key fields and included fields belong to one main, tabular or virtual "
        "table. Physical creation requires CORP, enabled additional-index usage and restructuring. "
        "ValueStorage/unlimited strings cannot be key fields; composite types occupy several SQL columns. "
        "Metadata cannot prove deployment, SQL names, optimizer use or manually created SQL indexes.",
    }
