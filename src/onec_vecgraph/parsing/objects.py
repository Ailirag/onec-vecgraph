"""Parse a single MetaDataObject XML file into a MetaObject, plus Rights/Predefined."""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from .model import (
    EnumValue,
    Field,
    FormRef,
    MetaObject,
    ModuleRef,
    Predefined,
    RoleRight,
    TabularSection,
)
from .ns import MD, PREDEF, ROLES, XR, ln, q, synonym, text
from .types import parse_type

# Object-module files that may sit next to an object as <ObjectDir>/Ext/*.bsl
_FORM_MODULE = ("Ext", "Form", "Module.bsl")

_COMMON_MODULE_FLAGS = (
    "Global",
    "Server",
    "ServerCall",
    "ClientManagedApplication",
    "ClientOrdinaryApplication",
    "ExternalConnection",
    "Privileged",
)


_RAW_MAX = 2000  # cap per structured property value (raw-XML fallback)


def _props(obj_el: etree._Element) -> etree._Element | None:
    return obj_el.find(q(MD, "Properties"))


def _flatten_properties(props_el: etree._Element | None) -> dict[str, str]:
    """Flatten an object's <Properties> into name->value for the detail layer.

    Leaf (scalar) properties keep their text value (Hierarchical, CodeLength, Posting,
    Periodicity, NumberLength, FullTextSearch, DataLockControlMode, ...). Structured
    properties (with child elements: StandardAttributes, InputByString, Characteristics, ...)
    keep a raw inner-XML snippet as a faithful fallback. Nothing semantic is dropped; the
    result is stored on a sidecar :Detail node and is NOT vectorized.
    """
    details: dict[str, str] = {}
    if props_el is None:
        return details
    for child in props_el:
        if not isinstance(child.tag, str):  # comments / processing instructions
            continue
        key = ln(child.tag)
        if len(child) == 0:
            value = (child.text or "").strip()
            if value:
                details[key] = value
        else:
            inner = "".join(etree.tostring(sub, encoding="unicode") for sub in child).strip()
            if inner:
                details[key] = inner if len(inner) <= _RAW_MAX else inner[:_RAW_MAX] + "…(truncated)"
    return details


def _children(obj_el: etree._Element) -> etree._Element | None:
    return obj_el.find(q(MD, "ChildObjects"))


def _parse_field(el: etree._Element, role: str, owner_fqn: str) -> Field:
    props = _props(el)
    name = text(props, q(MD, "Name"))
    type_desc = parse_type(props.find(q(MD, "Type")) if props is not None else None)
    return Field(
        name=name,
        role=role,
        fqn=f"{owner_fqn}.{role}.{name}",
        synonym=synonym(props),
        comment=text(props, q(MD, "Comment")),
        uuid=el.get("uuid"),
        types=type_desc.refs,
        type_text=type_desc.render(),
    )


def _parse_tabular(el: etree._Element, owner_fqn: str) -> TabularSection:
    props = _props(el)
    name = text(props, q(MD, "Name"))
    fqn = f"{owner_fqn}.TabularSection.{name}"
    ts = TabularSection(
        name=name,
        fqn=fqn,
        synonym=synonym(props),
        comment=text(props, q(MD, "Comment")),
        uuid=el.get("uuid"),
    )
    children = _children(el)
    if children is not None:
        for attr in children.findall(q(MD, "Attribute")):
            ts.fields.append(_parse_field(attr, "Attribute", fqn))
    return ts


def _scan_modules(object_dir: Path | None, obj_fqn: str) -> list[ModuleRef]:
    if object_dir is None or not object_dir.is_dir():
        return []
    modules: list[ModuleRef] = []
    ext = object_dir / "Ext"
    if ext.is_dir():
        for bsl in sorted(ext.glob("*.bsl")):
            modules.append(
                ModuleRef(
                    module_type=bsl.stem,
                    fqn=f"{obj_fqn}.Module.{bsl.stem}",
                    path=str(bsl),
                    size=bsl.stat().st_size,
                )
            )
    return modules


def _form_module_path(object_dir: Path | None, form_name: str) -> str | None:
    if object_dir is None:
        return None
    candidate = object_dir / "Forms" / form_name / Path(*_FORM_MODULE)
    return str(candidate) if candidate.is_file() else None


def _form_xml_path(object_dir: Path | None, form_name: str) -> str | None:
    if object_dir is None:
        return None
    candidate = object_dir / "Forms" / form_name / "Ext" / "Form.xml"
    return str(candidate) if candidate.is_file() else None


def _mdref_items(container: etree._Element | None) -> list[str]:
    """Read a list of <xr:Item ...>Kind.Name</xr:Item> references."""
    if container is None:
        return []
    out = []
    for item in container.findall(q(XR, "Item")):
        if item.text:
            out.append(item.text.strip())
    return out


def parse_object(
    obj_el: etree._Element, config_id: str, object_dir: Path | None, fqn: str | None = None
) -> MetaObject:
    kind = ln(obj_el.tag)
    props = _props(obj_el)
    name = text(props, q(MD, "Name"))
    fqn = fqn or f"{kind}.{name}"
    obj = MetaObject(
        kind=kind,
        name=name,
        fqn=fqn,
        config_id=config_id,
        synonym=synonym(props),
        comment=text(props, q(MD, "Comment")),
        uuid=obj_el.get("uuid"),
        belonging=text(props, q(MD, "ObjectBelonging"), "Own") or "Own",
    )
    obj.details = _flatten_properties(props)

    if kind == "CommonModule" and props is not None:
        for flag in _COMMON_MODULE_FLAGS:
            obj.flags[flag] = text(props, q(MD, flag))

    if kind == "Catalog" and props is not None:
        obj.owners = _mdref_items(props.find(q(MD, "Owners")))

    if kind == "Document" and props is not None:
        obj.register_records = _mdref_items(props.find(q(MD, "RegisterRecords")))

    if kind == "Subsystem" and props is not None:
        obj.content = _mdref_items(props.find(q(MD, "Content")))
        children = _children(obj_el)
        if children is not None:
            for sub in children.findall(q(MD, "Subsystem")):
                if sub.text:
                    obj.child_subsystems.append(f"{fqn}.Subsystem.{sub.text.strip()}")

    if kind == "EventSubscription" and props is not None:
        obj.source_types = parse_type(props.find(q(MD, "Source"))).refs
        obj.event = text(props, q(MD, "Event")) or None
        handler = text(props, q(MD, "Handler")) or None
        obj.handler_raw = handler
        if handler:
            parts = handler.split(".")
            if len(parts) >= 3 and parts[0] == "CommonModule":
                obj.handler_module_fqn = f"CommonModule.{parts[1]}"
                obj.handler_method = ".".join(parts[2:])

    children = _children(obj_el)
    if children is not None:
        for el in children:
            tag = ln(el.tag)
            if tag == "Attribute":
                obj.fields.append(_parse_field(el, "Attribute", fqn))
            elif tag == "Dimension":
                obj.fields.append(_parse_field(el, "Dimension", fqn))
            elif tag == "Resource":
                obj.fields.append(_parse_field(el, "Resource", fqn))
            elif tag == "TabularSection":
                obj.tabular.append(_parse_tabular(el, fqn))
            elif tag == "EnumValue":
                ep = _props(el)
                ev_name = text(ep, q(MD, "Name"))
                obj.enum_values.append(
                    EnumValue(
                        name=ev_name,
                        fqn=f"{fqn}.EnumValue.{ev_name}",
                        synonym=synonym(ep),
                        comment=text(ep, q(MD, "Comment")),
                        uuid=el.get("uuid"),
                    )
                )
            elif tag == "Form" and el.text:
                form_name = el.text.strip()
                obj.forms.append(
                    FormRef(
                        name=form_name,
                        fqn=f"{fqn}.Form.{form_name}",
                        module_path=_form_module_path(object_dir, form_name),
                        form_path=_form_xml_path(object_dir, form_name),
                    )
                )

    obj.modules = _scan_modules(object_dir, fqn)
    return obj


def parse_rights(path: Path) -> list[RoleRight]:
    """Parse Roles/<Role>/Ext/Rights.xml -> per-object rights."""
    tree = etree.parse(str(path))
    root = tree.getroot()
    out: list[RoleRight] = []
    for obj in root.findall(q(ROLES, "object")):
        obj_name = text(obj, q(ROLES, "name"))
        rights: dict[str, bool] = {}
        for r in obj.findall(q(ROLES, "right")):
            rname = text(r, q(ROLES, "name"))
            rval = text(r, q(ROLES, "value")).lower() == "true"
            if rname:
                rights[rname] = rval
        if obj_name:
            out.append(RoleRight(object_fqn=obj_name, rights=rights))
    return out


def parse_predefined(path: Path, owner_fqn: str) -> list[Predefined]:
    """Parse <Object>/Ext/Predefined.xml -> predefined items."""
    tree = etree.parse(str(path))
    root = tree.getroot()
    out: list[Predefined] = []
    for item in root.findall(q(PREDEF, "Item")):
        name = text(item, q(PREDEF, "Name"))
        if not name:
            continue
        out.append(
            Predefined(
                name=name,
                fqn=f"{owner_fqn}.Predefined.{name}",
                code=text(item, q(PREDEF, "Code")),
                description=text(item, q(PREDEF, "Description")),
                is_folder=text(item, q(PREDEF, "IsFolder")).lower() == "true",
                uuid=item.get("id"),
            )
        )
    return out
