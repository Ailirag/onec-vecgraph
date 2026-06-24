"""Parse a single EDT .mdo file into the shared MetaObject domain model.

Covers the structural graph: name/synonym/comment/belonging, attributes (incl. reference
types), dimensions/resources, tabular sections, enum values, subsystem content + child
subsystems, document register movements, catalog owners, common-module flags, forms (names
+ EDT module/form paths) and modules (paths). Predefined data and role rights are EDT-format
files (Predefined.* / Rights.rights) and are handled separately / TODO.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from ..model import EnumValue, Field, FormRef, MetaObject, ModuleRef, Predefined, TabularSection
from ..types import _REF_KIND_ALIASES, _REF_RE, TypeDescription, TypeRef
from .ns import child_text, child_texts, local_name, synonym

# Structural / already-captured child elements excluded from the flat :Detail property set.
_DETAIL_SKIP = {
    "name", "synonym", "comment", "objectBelonging", "uuid",
    "attributes", "dimensions", "resources", "tabularSections", "enumValues", "forms",
    "templates", "commands", "commandGroups", "owners", "registerRecords", "content",
    "subsystems", "producedTypes", "standardAttributes", "internalInfo", "source",
}

# EDT compilation/visibility flags on a CommonModule -> the same flag keys the rest of the
# pipeline already uses (chunking.role/common-module logic).
_COMMON_MODULE_FLAGS = {
    "global": "Global",
    "server": "Server",
    "serverCall": "ServerCall",
    "clientManagedApplication": "ClientManagedApplication",
    "clientOrdinaryApplication": "ClientOrdinaryApplication",
    "externalConnection": "ExternalConnection",
    "privileged": "Privileged",
}

# BSL module files that sit directly in an EDT object folder (no Ext/ wrapper).
_MODULE_STEMS = {
    "ObjectModule", "ManagerModule", "RecordSetModule", "ValueManagerModule",
    "CommandModule", "Module", "RecordManagerModule",
}


def _resolve_type(value: str) -> TypeRef:
    """EDT type entry text -> TypeRef. Reference entries are 'CatalogRef.Name' (no cfg: prefix)."""
    m = _REF_RE.match(value)
    if m:
        root = _REF_KIND_ALIASES.get(m.group("root"), m.group("root"))
        return TypeRef(value, "reference", root, m.group("name"))
    return TypeRef(raw=value, category="primitive")


def parse_edt_type(type_el: etree._Element | None) -> TypeDescription:
    """Parse an EDT <type> block (<types>…</types> entries) into a TypeDescription."""
    desc = TypeDescription()
    if type_el is None:
        return desc
    for entry in type_el.findall("types"):
        if entry.text and entry.text.strip():
            desc.refs.append(_resolve_type(entry.text.strip()))
    for entry in type_el.findall("typeSet"):
        if entry.text and entry.text.strip():
            ref = _resolve_type(entry.text.strip())
            ref.category = "typeset"
            desc.refs.append(ref)
    return desc


def _flatten_details(obj_el: etree._Element) -> dict[str, str]:
    """Flat scalar config/UI properties (Hierarchical, CodeLength, Posting, …) for the :Detail
    sidecar. Leaf-only (elements with text, no children); structural blocks are excluded."""
    details: dict[str, str] = {}
    for child in obj_el:
        if not isinstance(child.tag, str):
            continue
        key = local_name(child.tag)
        if key in _DETAIL_SKIP or len(child) > 0:
            continue
        value = (child.text or "").strip()
        if value:
            details[key] = value
    return details


def _parse_predefined(obj_el: etree._Element, owner_fqn: str) -> list[Predefined]:
    """EDT predefined data is inline in the .mdo <predefined> block: <items>/nested <content>
    nodes with name/code/description/isFolder. Collected recursively (folders + leaves)."""
    pre = obj_el.find("predefined")
    if pre is None:
        return []
    out: list[Predefined] = []

    def walk(parent: etree._Element) -> None:
        for el in list(parent.findall("items")) + list(parent.findall("content")):
            name = child_text(el, "name")
            if name:
                out.append(Predefined(
                    name=name, fqn=f"{owner_fqn}.Predefined.{name}",
                    code=child_text(el, "code"), description=child_text(el, "description"),
                    is_folder=child_text(el, "isFolder").lower() == "true", uuid=el.get("id")))
            walk(el)  # nested content (hierarchical predefined folders)

    walk(pre)
    return out


def _parse_field(el: etree._Element, role: str, owner_fqn: str) -> Field:
    name = child_text(el, "name")
    type_desc = parse_edt_type(el.find("type"))
    return Field(
        name=name,
        role=role,
        fqn=f"{owner_fqn}.{role}.{name}",
        synonym=synonym(el),
        comment=child_text(el, "comment"),
        uuid=el.get("uuid"),
        types=type_desc.refs,
        type_text=type_desc.render(),
    )


def _parse_tabular(el: etree._Element, owner_fqn: str) -> TabularSection:
    name = child_text(el, "name")
    fqn = f"{owner_fqn}.TabularSection.{name}"
    ts = TabularSection(
        name=name, fqn=fqn, synonym=synonym(el), comment=child_text(el, "comment"),
        uuid=el.get("uuid"),
    )
    for attr in el.findall("attributes"):
        ts.fields.append(_parse_field(attr, "Attribute", fqn))
    return ts


def _scan_modules(object_dir: Path | None, obj_fqn: str) -> list[ModuleRef]:
    """BSL modules sit directly in the EDT object folder (ObjectModule.bsl, ManagerModule.bsl…)."""
    if object_dir is None or not object_dir.is_dir():
        return []
    modules: list[ModuleRef] = []
    for bsl in sorted(object_dir.glob("*.bsl")):
        if bsl.stem not in _MODULE_STEMS:
            continue
        modules.append(
            ModuleRef(module_type=bsl.stem, fqn=f"{obj_fqn}.Module.{bsl.stem}",
                      path=str(bsl), size=bsl.stat().st_size)
        )
    return modules


def _form_paths(object_dir: Path | None, form_name: str) -> tuple[str | None, str | None]:
    """EDT form layout: <object>/Forms/<Name>/Form.form + Module.bsl (no Ext/ wrapper)."""
    if object_dir is None:
        return None, None
    base = object_dir / "Forms" / form_name
    module = base / "Module.bsl"
    form = base / "Form.form"
    return (str(module) if module.is_file() else None,
            str(form) if form.is_file() else None)


def parse_object(
    obj_el: etree._Element, config_id: str, object_dir: Path | None, kind: str, fqn: str
) -> MetaObject:
    name = child_text(obj_el, "name") or fqn.partition(".")[2]
    obj = MetaObject(
        kind=kind,
        name=name,
        fqn=fqn,
        config_id=config_id,
        synonym=synonym(obj_el),
        comment=child_text(obj_el, "comment"),
        uuid=obj_el.get("uuid"),
        belonging=child_text(obj_el, "objectBelonging") or "Own",
    )
    obj.details = _flatten_details(obj_el)

    if kind == "CommonModule":
        for edt_name, flag in _COMMON_MODULE_FLAGS.items():
            val = child_text(obj_el, edt_name)
            if val:
                obj.flags[flag] = val

    # Fields: attributes / dimensions / resources (registers carry the latter two).
    for attr in obj_el.findall("attributes"):
        obj.fields.append(_parse_field(attr, "Attribute", fqn))
    for dim in obj_el.findall("dimensions"):
        obj.fields.append(_parse_field(dim, "Dimension", fqn))
    for res in obj_el.findall("resources"):
        obj.fields.append(_parse_field(res, "Resource", fqn))

    for ts in obj_el.findall("tabularSections"):
        obj.tabular.append(_parse_tabular(ts, fqn))

    for ev in obj_el.findall("enumValues"):
        ev_name = child_text(ev, "name")
        obj.enum_values.append(EnumValue(
            name=ev_name, fqn=f"{fqn}.EnumValue.{ev_name}",
            synonym=synonym(ev), comment=child_text(ev, "comment"), uuid=ev.get("uuid"),
        ))

    for fm in obj_el.findall("forms"):
        form_name = child_text(fm, "name")
        if not form_name:
            continue
        module_path, form_path = _form_paths(object_dir, form_name)
        obj.forms.append(FormRef(name=form_name, fqn=f"{fqn}.Form.{form_name}",
                                 module_path=module_path, form_path=form_path))

    # Reference lists (plain 'Kind.Name' text refs).
    obj.owners = child_texts(obj_el, "owners")
    obj.register_records = child_texts(obj_el, "registerRecords")
    obj.content = child_texts(obj_el, "content")
    for child in child_texts(obj_el, "subsystems"):
        obj.child_subsystems.append(f"{fqn}.Subsystem.{child}")

    # Event subscription: source types + handler (CommonModule.X.Method).
    src = obj_el.find("source")
    if src is not None:
        obj.source_types = parse_edt_type(src).refs
    obj.event = child_text(obj_el, "event") or None
    handler = child_text(obj_el, "handler") or None
    if handler:
        obj.handler_raw = handler
        parts = handler.split(".")
        if len(parts) >= 3 and parts[0] == "CommonModule":
            obj.handler_module_fqn = f"CommonModule.{parts[1]}"
            obj.handler_method = ".".join(parts[2:])

    obj.predefined = _parse_predefined(obj_el, fqn)
    obj.modules = _scan_modules(object_dir, fqn)
    return obj
