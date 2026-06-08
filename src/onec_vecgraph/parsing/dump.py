"""Discover configuration parts (base + extensions) in a Configurator dump and parse them."""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from . import objects as obj_parser
from .dumpinfo import parse_dump_info
from .model import ConfigPart, MetaObject, ParsedConfig
from .ns import MD, first_child_element, q, synonym, text

log = logging.getLogger(__name__)

# Plural dump folder -> metadata object kind.
TYPE_FOLDERS: dict[str, str] = {
    "Catalogs": "Catalog",
    "Documents": "Document",
    "DocumentJournals": "DocumentJournal",
    "Enums": "Enum",
    "Reports": "Report",
    "DataProcessors": "DataProcessor",
    "InformationRegisters": "InformationRegister",
    "AccumulationRegisters": "AccumulationRegister",
    "AccountingRegisters": "AccountingRegister",
    "CalculationRegisters": "CalculationRegister",
    "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
    "ChartsOfAccounts": "ChartOfAccounts",
    "ChartsOfCalculationTypes": "ChartOfCalculationTypes",
    "BusinessProcesses": "BusinessProcess",
    "Tasks": "Task",
    "CommonModules": "CommonModule",
    "Roles": "Role",
    "Constants": "Constant",
    "ExchangePlans": "ExchangePlan",
    "EventSubscriptions": "EventSubscription",
    "ScheduledJobs": "ScheduledJob",
    "SessionParameters": "SessionParameter",
    "CommonPictures": "CommonPicture",
    "CommonTemplates": "CommonTemplate",
    "CommonForms": "CommonForm",
    "CommonCommands": "CommonCommand",
    "CommonAttributes": "CommonAttribute",
    "DefinedTypes": "DefinedType",
    "FunctionalOptions": "FunctionalOption",
    "FunctionalOptionsParameters": "FunctionalOptionsParameter",
    "Languages": "Language",
    "WebServices": "WebService",
    "HTTPServices": "HTTPService",
    "WSReferences": "WSReference",
    "FilterCriteria": "FilterCriterion",
    "SettingsStorages": "SettingsStorage",
    "CommandGroups": "CommandGroup",
    "DocumentNumerators": "DocumentNumerator",
    "StyleItems": "StyleItem",
    "Interfaces": "Interface",
    "XDTOPackages": "XDTOPackage",
    "ExternalDataSources": "ExternalDataSource",
    "IntegrationServices": "IntegrationService",
    "Subsystems": "Subsystem",
}


def discover_parts(root: Path) -> list[ConfigPart]:
    """Find directories that contain a Configuration.xml (base + extensions)."""
    candidates: list[Path] = []
    if (root / "Configuration.xml").is_file():
        candidates.append(root)
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if (child / "Configuration.xml").is_file():
            candidates.append(child)

    parts = [_read_part(d) for d in candidates]
    # Base first, then extensions (stable order for MERGE-by-fqn semantics).
    parts.sort(key=lambda p: (p.is_extension, p.name))
    return parts


def _read_part(part_dir: Path) -> ConfigPart:
    tree = etree.parse(str(part_dir / "Configuration.xml"))
    cfg = first_child_element(tree.getroot())  # <Configuration>
    props = cfg.find(q(MD, "Properties")) if cfg is not None else None
    name = text(props, q(MD, "Name"))
    purpose = text(props, q(MD, "ConfigurationExtensionPurpose")) or None
    is_ext = purpose is not None or props.find(q(MD, "ObjectBelonging")) is not None
    return ConfigPart(
        config_id=f"ext:{name}" if is_ext else "base",
        name=name,
        root_dir=str(part_dir),
        is_extension=is_ext,
        purpose=purpose,
        name_prefix=text(props, q(MD, "NamePrefix")),
        uuid=cfg.get("uuid") if cfg is not None else None,
        synonym=synonym(props),
    )


def _iter_subsystems(folder: Path, parent_fqn: str, acc: list[tuple[str, Path, Path]]) -> None:
    """Yield subsystems with qualified fqns matching ConfigDumpInfo
    ('Subsystem.Parent.Subsystem.Child')."""
    for xml in sorted(folder.glob("*.xml")):
        name = xml.stem
        fqn = f"{parent_fqn}.Subsystem.{name}" if parent_fqn else f"Subsystem.{name}"
        object_dir = folder / name
        acc.append((fqn, xml, object_dir))
        nested = object_dir / "Subsystems"
        if nested.is_dir():
            _iter_subsystems(nested, fqn, acc)


def _iter_object_files(part_dir: Path) -> list[tuple[str, Path, Path]]:
    """List (fqn, xml, object_dir) for every metadata object file in a part."""
    files: list[tuple[str, Path, Path]] = []
    for folder_name, kind in TYPE_FOLDERS.items():
        folder = part_dir / folder_name
        if not folder.is_dir():
            continue
        if folder_name == "Subsystems":
            _iter_subsystems(folder, "", files)
        else:
            for xml in sorted(folder.glob("*.xml")):
                files.append((f"{kind}.{xml.stem}", xml, folder / xml.stem))
    return files


def enumerate_objects(parts: list[ConfigPart]) -> list[tuple[str, Path, Path, str, str | None]]:
    """Cheaply list (fqn, xml, object_dir, config_id, config_version) without parsing XML."""
    out: list[tuple[str, Path, Path, str, str | None]] = []
    for part in parts:
        part_dir = Path(part.root_dir)
        versions = parse_dump_info(part_dir)
        for fqn, xml, object_dir in _iter_object_files(part_dir):
            out.append((fqn, xml, object_dir, part.config_id, versions.get(fqn)))
    return out


def _parse_object_file(
    fqn: str, xml: Path, object_dir: Path, config_id: str, config_version: str | None = None
) -> MetaObject | None:
    tree = etree.parse(str(xml))
    obj_el = first_child_element(tree.getroot())  # the typed object element
    if obj_el is None:
        return None
    obj = obj_parser.parse_object(obj_el, config_id, object_dir, fqn=fqn)
    obj.config_version = config_version
    if obj.kind == "Role":
        rights_file = object_dir / "Ext" / "Rights.xml"
        if rights_file.is_file():
            obj.rights = obj_parser.parse_rights(rights_file)
    predefined_file = object_dir / "Ext" / "Predefined.xml"
    if predefined_file.is_file():
        obj.predefined = obj_parser.parse_predefined(predefined_file, obj.fqn)
    return obj


def _parse_into(parsed: ParsedConfig, refs: list[tuple[str, Path, Path, str, str | None]]) -> None:
    for fqn, xml, object_dir, config_id, version in refs:
        parsed.files_seen += 1
        try:
            obj = _parse_object_file(fqn, xml, object_dir, config_id, version)
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the import
            log.warning("Failed to parse %s", xml, exc_info=True)
            parsed.errors.append(f"{xml}: {exc!r}")
            continue
        if obj is not None:
            parsed.objects.append(obj)


def parse_config(root: Path, tenant_id: str) -> ParsedConfig:
    parts = discover_parts(root)
    parsed = ParsedConfig(tenant_id=tenant_id, parts=parts)
    _parse_into(parsed, enumerate_objects(parts))
    return parsed


def parse_objects(
    tenant_id: str, parts: list[ConfigPart], refs: list[tuple[str, Path, Path, str, str | None]]
) -> ParsedConfig:
    """Parse only the given object refs (incremental indexing)."""
    parsed = ParsedConfig(tenant_id=tenant_id, parts=parts)
    _parse_into(parsed, refs)
    return parsed
