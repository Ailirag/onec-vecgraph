"""Domain model for parsed 1C metadata (graph-ready, transport-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import TypeRef


@dataclass
class Field:
    """An attribute, dimension or resource (also tabular-section attributes)."""

    name: str
    role: str  # 'Attribute' | 'Dimension' | 'Resource'
    fqn: str
    synonym: str = ""
    comment: str = ""
    uuid: str | None = None
    types: list[TypeRef] = field(default_factory=list)
    type_text: str = ""


@dataclass
class TabularSection:
    name: str
    fqn: str
    synonym: str = ""
    comment: str = ""
    uuid: str | None = None
    fields: list[Field] = field(default_factory=list)


@dataclass
class EnumValue:
    name: str
    fqn: str
    synonym: str = ""
    comment: str = ""
    uuid: str | None = None


@dataclass
class Predefined:
    name: str
    fqn: str
    code: str = ""
    description: str = ""
    is_folder: bool = False
    uuid: str | None = None


@dataclass
class FormRef:
    name: str
    fqn: str
    module_path: str | None = None
    form_path: str | None = None  # Ext/Form.xml (for vectorizing form content)


@dataclass
class ModuleRef:
    module_type: str  # Module | ObjectModule | ManagerModule | FormModule | ...
    fqn: str
    path: str
    size: int = 0


@dataclass
class RoleRight:
    object_fqn: str
    rights: dict[str, bool] = field(default_factory=dict)


@dataclass
class MetaObject:
    kind: str
    name: str
    fqn: str
    config_id: str
    synonym: str = ""
    comment: str = ""
    uuid: str | None = None
    belonging: str = "Own"  # Own | Adopted (extension)
    config_version: str | None = None  # configVersion hash (incremental indexing)
    flags: dict[str, str] = field(default_factory=dict)

    # Full <Properties> set (every config/UI property: Hierarchical, CodeLength, Posting,
    # Periodicity, FullTextSearch, ...). Stored on a sidecar :Detail node for dev/analyst
    # lookup; deliberately NOT vectorized (would dilute semantic search).
    details: dict[str, str] = field(default_factory=dict)

    fields: list[Field] = field(default_factory=list)
    tabular: list[TabularSection] = field(default_factory=list)
    enum_values: list[EnumValue] = field(default_factory=list)
    predefined: list[Predefined] = field(default_factory=list)
    forms: list[FormRef] = field(default_factory=list)
    modules: list[ModuleRef] = field(default_factory=list)

    owners: list[str] = field(default_factory=list)  # target fqns (Catalog owners)

    # Document: registers this document posts movements to (RegisterRecords)
    register_records: list[str] = field(default_factory=list)

    # Subsystem
    content: list[str] = field(default_factory=list)  # member fqns
    child_subsystems: list[str] = field(default_factory=list)  # child subsystem fqns

    # EventSubscription
    source_types: list[TypeRef] = field(default_factory=list)
    event: str | None = None
    handler_module_fqn: str | None = None
    handler_method: str | None = None
    handler_raw: str | None = None

    # Role
    rights: list[RoleRight] = field(default_factory=list)


@dataclass
class ConfigPart:
    """A configuration part: the base config or one extension."""

    config_id: str  # 'base' or 'ext:<Name>'
    name: str
    root_dir: str
    is_extension: bool = False
    purpose: str | None = None  # Customization | Patch | AddOn ...
    name_prefix: str = ""
    uuid: str | None = None
    synonym: str = ""
    fmt: str = "configurator"  # source format: 'configurator' (XML dump) | 'edt'


@dataclass
class ParsedConfig:
    tenant_id: str
    parts: list[ConfigPart] = field(default_factory=list)
    objects: list[MetaObject] = field(default_factory=list)
    files_seen: int = 0
    errors: list[str] = field(default_factory=list)
