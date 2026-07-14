"""Structure views over single metadata files: services (HTTP/Web) and managed forms.

Format-agnostic by local-name matching: the Configurator dump wraps everything in
<Properties>/<ChildObjects> (CamelCase tags), EDT uses flat lowerCamel children — the
`_prop` helper reads both. Parsed on demand from one file, no workspace state here.

Verified against real files: EDT `ut` workspace (HTTPService Биллинг, WebService
DMILService, Form.form РеализацияТоваровУслуг) and Configurator dumps (ERP `main`
HTTPServices/WebServices, demo Ext/Form.xml)."""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from ..parsing.ns import first_child_element, ln


def _parse_root(path: Path) -> etree._Element | None:
    try:
        root = etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return None
    # Configurator files wrap the typed element in <MetaDataObject>.
    if ln(root.tag) == "MetaDataObject":
        return first_child_element(root)
    return root


def _children(el: etree._Element, *names: str) -> list[etree._Element]:
    """Direct children matching local-names, looking through Configurator <ChildObjects>."""
    wanted = {n.lower() for n in names}
    out: list[etree._Element] = []
    for c in el:
        tag = ln(c.tag).lower()
        if tag in wanted:
            out.append(c)
        elif tag == "childobjects":
            out.extend(x for x in c if ln(x.tag).lower() in wanted)
    return out


def _prop(el: etree._Element, *names: str) -> str:
    """Text of a direct child by local-name; Configurator's <Properties> is transparent."""
    wanted = {n.lower() for n in names}
    for c in el:
        tag = ln(c.tag).lower()
        if tag in wanted and c.text:
            return c.text.strip()
        if tag == "properties":
            for p in c:
                if ln(p.tag).lower() in wanted and p.text:
                    return p.text.strip()
    return ""


def _ru_value(el: etree._Element | None) -> str:
    """Multilingual value: EDT <key>ru</key><value>..</value>, Configurator v8:item/lang/content."""
    if el is None:
        return ""
    pairs: list[tuple[str, str]] = []
    lang = ""
    for node in el.iter():
        tag = ln(node.tag).lower()
        if tag in ("key", "lang"):
            lang = (node.text or "").strip()
        elif tag in ("value", "content"):
            pairs.append((lang, (node.text or "").strip()))
    for code, value in pairs:
        if code == "ru" and value:
            return value
    return pairs[0][1] if pairs else ""


def _title(el: etree._Element) -> str:
    for c in el:
        if ln(c.tag).lower() in ("title", "synonym"):
            return _ru_value(c)
    return ""


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #

def parse_http_service(path: Path) -> dict | None:
    el = _parse_root(path)
    if el is None or ln(el.tag) != "HTTPService":
        return None
    templates = []
    for t in _children(el, "urlTemplates", "URLTemplate"):
        methods = [
            {
                "name": _prop(m, "name"),
                "http_method": _prop(m, "httpMethod", "HTTPMethod") or "ANY",
                "handler": _prop(m, "handler"),
            }
            for m in _children(t, "methods", "Method")
        ]
        templates.append({
            "name": _prop(t, "name"),
            "template": _prop(t, "template"),
            "methods": methods,
        })
    return {
        "kind": "HTTPService",
        "name": _prop(el, "name"),
        "root_url": _prop(el, "rootURL"),
        "reuse_sessions": _prop(el, "reuseSessions"),
        "url_templates": templates,
    }


def parse_web_service(path: Path) -> dict | None:
    el = _parse_root(path)
    if el is None or ln(el.tag) != "WebService":
        return None
    operations = []
    for op in _children(el, "operations", "Operation"):
        params = [
            {
                "name": _prop(p, "name"),
                "type": _first_type_name(p, "xdtoValueType", "XDTOValueType"),
            }
            for p in _children(op, "parameters", "Parameter")
        ]
        operations.append({
            "name": _prop(op, "name"),
            "handler": _prop(op, "procedureName", "ProcedureName"),
            "returns": _first_type_name(op, "xdtoReturningValueType", "XDTOReturningValueType"),
            "parameters": params,
        })
    return {
        "kind": "WebService",
        "name": _prop(el, "name"),
        "namespace": _prop(el, "namespace"),
        "descriptor_file": _prop(el, "descriptorFileName"),
        "operations": operations,
    }


def _first_type_name(el: etree._Element, *containers: str) -> str:
    wanted = {c.lower() for c in containers}
    for c in el:
        if ln(c.tag).lower() in wanted:
            name = _prop(c, "name")
            if name:
                return name
            return (c.text or "").strip()
    return ""


# --------------------------------------------------------------------------- #
# Managed forms
# --------------------------------------------------------------------------- #

_MAX_ITEMS = 500


def parse_form(path: Path) -> dict:
    """Uniform form structure for EDT .form and Configurator Ext/Form.xml."""
    el = _parse_root(path)
    if el is None:
        return {"error": f"Не удалось разобрать форму: {path}"}
    if str(path).endswith(".form"):
        return _parse_form_edt(el)
    return _parse_form_configurator(el)


def _dedup_keep_order(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for r in rows:
        key = repr(sorted(r.items()))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _parse_form_edt(root: etree._Element) -> dict:
    attributes = []
    for a in root:
        if ln(a.tag) != "attributes":
            continue
        types = [t.text.strip() for t in a.iter() if ln(t.tag) == "types" and t.text]
        attributes.append({
            "name": _prop(a, "name"),
            "title": _title(a),
            "type": ", ".join(types),
        })

    commands = []
    for c in root:
        if ln(c.tag) != "formCommands":
            continue
        handler = ""
        for action in c:
            if ln(action.tag) == "action":
                handler = _prop(action, "handler") or (action.text or "").strip()
                for h in action:
                    if ln(h.tag) == "handler":
                        handler = _prop(h, "name") or (h.text or "").strip()
        commands.append({
            "name": _prop(c, "name"),
            "title": _title(c),
            "shortcut": _prop(c, "shortcut"),
            "handler": handler,
        })

    items: list[dict] = []
    truncated = False

    def walk(el: etree._Element, trail: str) -> None:
        nonlocal truncated
        for child in el:
            if ln(child.tag) != "items":
                continue
            if len(items) >= _MAX_ITEMS:
                truncated = True
                return
            name = _prop(child, "name")
            xsi = child.get("{http://www.w3.org/2001/XMLSchema-instance}type") or ""
            row: dict = {
                "name": name,
                "kind": xsi.split(":")[-1] or "Item",
                "path": trail,
            }
            for sub in child:
                tag = ln(sub.tag)
                if tag == "dataPath":
                    segs = [s.text.strip() for s in sub.iter() if ln(s.tag) == "segments" and s.text]
                    row["data_path"] = ".".join(segs) or (sub.text or "").strip()
                elif tag == "commandName" and sub.text:
                    row["command"] = sub.text.strip().split(".")[-1]
                elif tag == "type" and sub.text:
                    row["type"] = sub.text.strip()
            handlers = [
                {"event": _prop(h, "event"), "handler": _prop(h, "name")}
                for h in child.iter() if ln(h.tag) == "handlers"
            ]
            if handlers:
                row["handlers"] = handlers
            items.append(row)
            walk(child, f"{trail}/{name}" if trail else name)

    walk(root, "")

    form_handlers = [
        {"event": _prop(h, "event"), "handler": _prop(h, "name")}
        for h in root if ln(h.tag) == "handlers"
    ]
    return {
        "attributes": attributes,
        "commands": commands,
        "items": items,
        "items_truncated": truncated,
        "form_handlers": form_handlers,
    }


def _parse_form_configurator(root: etree._Element) -> dict:
    attributes = []
    commands = []
    items: list[dict] = []
    truncated = False
    form_handlers: list[dict] = []

    for section in root:
        tag = ln(section.tag)
        if tag == "Attributes":
            for a in section:
                if ln(a.tag) != "Attribute":
                    continue
                types = [t.text.strip() for t in a.iter()
                         if ln(t.tag) == "Type" and t.text and t.text.strip()]
                main = any(ln(m.tag) == "MainAttribute" and (m.text or "").strip() == "true"
                           for m in a)
                row = {"name": a.get("name", ""), "title": _title(a), "type": ", ".join(types)}
                if main:
                    row["main"] = True
                attributes.append(row)
        elif tag == "Commands":
            for c in section:
                if ln(c.tag) != "Command":
                    continue
                commands.append({
                    "name": c.get("name", ""),
                    "title": _title(c),
                    "shortcut": "",
                    "handler": _prop(c, "Action"),
                })
        elif tag == "Events":
            for ev in section:
                if ln(ev.tag) == "Event" and ev.text:
                    form_handlers.append({"event": ev.get("name", ""),
                                          "handler": ev.text.strip()})

    def walk(el: etree._Element, trail: str) -> None:
        nonlocal truncated
        for child_items in el:
            if ln(child_items.tag) != "ChildItems":
                continue
            for item in child_items:
                if not isinstance(item.tag, str):
                    continue
                if len(items) >= _MAX_ITEMS:
                    truncated = True
                    return
                name = item.get("name", "")
                row: dict = {"name": name, "kind": ln(item.tag), "path": trail}
                for sub in item:
                    tag = ln(sub.tag)
                    if tag == "DataPath" and sub.text:
                        row["data_path"] = sub.text.strip()
                    elif tag == "CommandName" and sub.text:
                        row["command"] = sub.text.strip().split(".")[-1]
                    elif tag == "Events":
                        hs = [{"event": e.get("name", ""), "handler": e.text.strip()}
                              for e in sub if ln(e.tag) == "Event" and e.text]
                        if hs:
                            row["handlers"] = hs
                items.append(row)
                walk(item, f"{trail}/{name}" if trail else name)

    walk(root, "")
    return {
        "attributes": attributes,
        "commands": commands,
        "items": items,
        "items_truncated": truncated,
        "form_handlers": _dedup_keep_order(form_handlers),
    }
