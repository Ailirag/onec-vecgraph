"""Convert a ParsedConfig into batched Neo4j node/edge groups."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..parsing.model import Field, MetaObject, ParsedConfig

_ROLE_REL = {"Attribute": "HAS_ATTRIBUTE", "Dimension": "HAS_DIMENSION", "Resource": "HAS_RESOURCE"}


@dataclass
class EdgeGroup:
    rel: str
    src_label: str
    dst_label: str
    soft: bool  # soft = MERGE the destination (may be external/not-yet-created)
    rows: list[dict] = field(default_factory=list)


@dataclass
class GraphData:
    tenant_id: str
    nodes: dict[str, list[dict]] = field(default_factory=dict)
    edges: dict[tuple, EdgeGroup] = field(default_factory=dict)

    def edge_groups(self) -> list[EdgeGroup]:
        return list(self.edges.values())


def _split_fqn(fqn: str) -> tuple[str, str]:
    kind, sep, name = fqn.partition(".")
    if not sep:  # not a 'Kind.Name' ref (e.g. a dangling reference stored by UUID)
        return "Unresolved", fqn
    return kind, name


def _top_level(fqn: str) -> str:
    """Top-level metadata object fqn: 'Catalog.X.Attribute.Y' -> 'Catalog.X'."""
    parts = fqn.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else fqn


class _Builder:
    def __init__(self, tenant_id: str) -> None:
        self.data = GraphData(tenant_id=tenant_id)
        self._seen: set[tuple[str, str]] = set()

    def node(self, label: str, fqn: str, props: dict) -> None:
        if (label, fqn) in self._seen:
            return
        self._seen.add((label, fqn))
        row = {"fqn": fqn, "tenant_id": self.data.tenant_id, **props}
        self.data.nodes.setdefault(label, []).append(row)

    def edge(
        self,
        rel: str,
        src_label: str,
        src: str,
        dst_label: str,
        dst: str,
        soft: bool,
        props: dict | None = None,
    ) -> None:
        key = (rel, src_label, dst_label, soft)
        group = self.data.edges.get(key)
        if group is None:
            group = EdgeGroup(rel, src_label, dst_label, soft)
            self.data.edges[key] = group
        dst_kind, dst_name = _split_fqn(dst)
        group.rows.append(
            {"src": src, "dst": dst, "dst_kind": dst_kind, "dst_name": dst_name, "props": props or {}}
        )

    def _field(self, owner_label: str, owner_fqn: str, fld: Field, config_id: str) -> None:
        refs = [t for t in fld.types if t.category == "reference" and t.ref_fqn]
        self.node(
            "Field",
            fld.fqn,
            {
                "name": fld.name,
                "role": fld.role,
                "synonym": fld.synonym,
                "comment": fld.comment,
                "uuid": fld.uuid,
                "type_text": fld.type_text,
                "is_reference": bool(refs),
                "config_id": config_id,
            },
        )
        self.edge(_ROLE_REL.get(fld.role, "HAS_ATTRIBUTE"), owner_label, owner_fqn, "Field", fld.fqn, soft=False)
        for ref in refs:
            self.edge("REFERENCES", "Field", fld.fqn, "Object", ref.ref_fqn, soft=True)

    def object(self, obj: MetaObject) -> None:
        props = {
            "kind": obj.kind,
            "name": obj.name,
            "synonym": obj.synonym,
            "comment": obj.comment,
            "uuid": obj.uuid,
            "config_id": obj.config_id,
            "belonging": obj.belonging,
            "config_version": obj.config_version,
        }
        for flag, value in obj.flags.items():
            props[f"flag_{flag}"] = value
        self.node("Object", obj.fqn, props)

        # Sidecar detail node: full property set for dev/analyst lookup (not vectorized).
        if obj.details:
            self.node("Detail", obj.fqn, {"config_id": obj.config_id, **obj.details})
            self.edge("HAS_DETAIL", "Object", obj.fqn, "Detail", obj.fqn, soft=False)

        for fld in obj.fields:
            self._field("Object", obj.fqn, fld, obj.config_id)

        for ts in obj.tabular:
            self.node(
                "TabularSection",
                ts.fqn,
                {"name": ts.name, "synonym": ts.synonym, "comment": ts.comment,
                 "uuid": ts.uuid, "config_id": obj.config_id},
            )
            self.edge("HAS_TABULAR_SECTION", "Object", obj.fqn, "TabularSection", ts.fqn, soft=False)
            for fld in ts.fields:
                self._field("TabularSection", ts.fqn, fld, obj.config_id)

        for ev in obj.enum_values:
            self.node("EnumValue", ev.fqn,
                      {"name": ev.name, "synonym": ev.synonym, "comment": ev.comment,
                       "uuid": ev.uuid, "config_id": obj.config_id})
            self.edge("HAS_ENUM_VALUE", "Object", obj.fqn, "EnumValue", ev.fqn, soft=False)

        for pd in obj.predefined:
            self.node("Predefined", pd.fqn,
                      {"name": pd.name, "code": pd.code, "description": pd.description,
                       "is_folder": pd.is_folder, "uuid": pd.uuid, "config_id": obj.config_id})
            self.edge("HAS_PREDEFINED", "Object", obj.fqn, "Predefined", pd.fqn, soft=False)

        for fm in obj.forms:
            self.node("Form", fm.fqn,
                      {"name": fm.name, "module_path": fm.module_path, "form_path": fm.form_path,
                       "config_id": obj.config_id})
            self.edge("HAS_FORM", "Object", obj.fqn, "Form", fm.fqn, soft=False)

        for md in obj.modules:
            self.node("Module", md.fqn,
                      {"module_type": md.module_type, "path": md.path, "size": md.size,
                       "config_id": obj.config_id})
            self.edge("HAS_MODULE", "Object", obj.fqn, "Module", md.fqn, soft=False)

        for owner_fqn in obj.owners:
            self.edge("OWNED_BY", "Object", obj.fqn, "Object", owner_fqn, soft=True)

        for reg_fqn in obj.register_records:
            self.edge("WRITES_TO", "Object", obj.fqn, "Object", reg_fqn, soft=True)

        for member in obj.content:
            self.edge("CONTAINS", "Object", obj.fqn, "Object", member, soft=True)

        for child in obj.child_subsystems:
            self.edge("HAS_SUBSYSTEM", "Object", obj.fqn, "Object", child, soft=True)

        for st in obj.source_types:
            if st.ref_fqn:
                self.edge("SUBSCRIBES", "Object", obj.fqn, "Object", st.ref_fqn, soft=True,
                          props={"event": obj.event})
        if obj.handler_module_fqn:
            self.edge("HANDLED_BY", "Object", obj.fqn, "Object", obj.handler_module_fqn,
                      soft=True, props={"method": obj.handler_method, "handler": obj.handler_raw})

        # Rights are declared per object AND per sub-object (attribute/form/command).
        # Collapse to the top-level object and union the granted rights.
        rights_by_object: dict[str, set[str]] = {}
        for rr in obj.rights:
            target = _top_level(rr.object_fqn)
            granted = rights_by_object.setdefault(target, set())
            granted.update(k for k, v in rr.rights.items() if v)
        for target, granted in rights_by_object.items():
            self.edge("HAS_RIGHT_ON", "Object", obj.fqn, "Object", target, soft=True,
                      props={"granted": sorted(granted)})


def build_graph(parsed: ParsedConfig) -> GraphData:
    builder = _Builder(parsed.tenant_id)
    for obj in parsed.objects:
        builder.object(obj)
    return builder.data
