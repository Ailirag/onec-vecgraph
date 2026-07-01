"""Discover EDT projects (base + extensions) and parse their .mdo objects.

An EDT *project* is a directory with `src/Configuration/Configuration.mdo` (and usually
`DT-INF/`). `root` may be a single project, or a workspace holding the base project
(e.g. `conf/`) alongside extension projects as sibling folders.

EDT has no ConfigDumpInfo, so the per-object incremental version is the content hash of the
.mdo file (modules are added to the hash in a later phase — see PLAN.md §17 R2.5).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from lxml import etree

from ...progress import Progress
from ..dump import TYPE_FOLDERS  # plural-folder -> kind map (identical across formats)
from ..model import ConfigPart, MetaObject, ParsedConfig
from ..objects import parse_rights  # EDT Rights.rights uses the same 'roles' namespace/schema
from . import objects as edt_objects
from .ns import child_text, synonym

log = logging.getLogger(__name__)

ObjectRef = tuple[str, Path, Path, str, str | None]


def is_edt_project(d: Path) -> bool:
    return (d / "src" / "Configuration" / "Configuration.mdo").is_file()


def _read_project(proj_dir: Path) -> ConfigPart:
    root = etree.parse(str(proj_dir / "src" / "Configuration" / "Configuration.mdo")).getroot()
    name = child_text(root, "name")
    purpose = child_text(root, "configurationExtensionPurpose") or None
    is_ext = purpose is not None
    return ConfigPart(
        config_id=f"ext:{name}" if is_ext else "base",
        name=name,
        root_dir=str(proj_dir),
        is_extension=is_ext,
        purpose=purpose,
        name_prefix=child_text(root, "namePrefix"),
        uuid=root.get("uuid"),
        synonym=synonym(root),
        fmt="edt",
    )


def discover_parts(root: Path) -> list[ConfigPart]:
    """Find EDT projects: `root` itself, or its immediate child project folders (base + extensions)."""
    candidates: list[Path] = []
    if is_edt_project(root):
        candidates.append(root)
    else:
        candidates = [c for c in sorted(p for p in root.iterdir() if p.is_dir()) if is_edt_project(c)]
    parts = [_read_project(d) for d in candidates]
    parts.sort(key=lambda p: (p.is_extension, p.name))  # base first (MERGE-by-fqn semantics)
    return parts


def _iter_subsystems(folder: Path, parent_fqn: str, acc: list[tuple[str, Path, Path]]) -> None:
    """Subsystems are nested folders; qualify fqns as 'Subsystem.Parent.Subsystem.Child'."""
    for sub_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        name = sub_dir.name
        mdo = sub_dir / f"{name}.mdo"
        if not mdo.is_file():
            continue
        fqn = f"{parent_fqn}.Subsystem.{name}" if parent_fqn else f"Subsystem.{name}"
        acc.append((fqn, mdo, sub_dir))
        nested = sub_dir / "Subsystems"
        if nested.is_dir():
            _iter_subsystems(nested, fqn, acc)


def _iter_object_files(src_dir: Path) -> list[tuple[str, Path, Path]]:
    """List (fqn, mdo_path, object_dir) for every EDT metadata object in a project's src/."""
    files: list[tuple[str, Path, Path]] = []
    for folder_name, kind in TYPE_FOLDERS.items():
        folder = src_dir / folder_name
        if not folder.is_dir():
            continue
        if folder_name == "Subsystems":
            _iter_subsystems(folder, "", files)
        else:
            for obj_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
                mdo = obj_dir / f"{obj_dir.name}.mdo"
                if mdo.is_file():
                    files.append((f"{kind}.{obj_dir.name}", mdo, obj_dir))
    return files


def _file_hash(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def enumerate_objects(parts: list[ConfigPart]) -> list[ObjectRef]:
    """Cheaply list (fqn, mdo, object_dir, config_id, content_hash) without parsing XML."""
    out: list[ObjectRef] = []
    for part in parts:
        src = Path(part.root_dir) / "src"
        for fqn, mdo, obj_dir in _iter_object_files(src):
            out.append((fqn, mdo, obj_dir, part.config_id, _file_hash(mdo)))
    return out


def _parse_object_file(fqn: str, mdo: Path, object_dir: Path, config_id: str,
                       version: str | None) -> MetaObject | None:
    obj_el = etree.parse(str(mdo)).getroot()  # the .mdo root IS the typed object element
    if obj_el is None:
        return None
    kind = fqn.partition(".")[0]
    obj = edt_objects.parse_object(obj_el, config_id, object_dir, kind, fqn)
    obj.config_version = version
    if kind == "Role":
        rights_file = object_dir / "Rights.rights"
        if rights_file.is_file():
            obj.rights = parse_rights(rights_file)
    return obj


def _parse_into(parsed: ParsedConfig, refs: list[ObjectRef], progress_label: str | None) -> None:
    prog = Progress(len(refs), progress_label, unit="объектов", rate_word="объект/с") if progress_label else None
    for fqn, mdo, object_dir, config_id, version in refs:
        parsed.files_seen += 1
        try:
            obj = _parse_object_file(fqn, mdo, object_dir, config_id, version)
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the import
            log.warning("Failed to parse %s", mdo, exc_info=True)
            parsed.errors.append(f"{mdo}: {exc!r}")
            obj = None
        if obj is not None:
            parsed.objects.append(obj)
        if prog:
            prog.advance()
    if prog:
        prog.finish()


def parse_objects(tenant_id: str, parts: list[ConfigPart], refs: list[ObjectRef],
                  progress_label: str | None = None) -> ParsedConfig:
    parsed = ParsedConfig(tenant_id=tenant_id, parts=parts)
    _parse_into(parsed, refs, progress_label)
    return parsed


def parse_config(root: Path, tenant_id: str, progress_label: str | None = None) -> ParsedConfig:
    parts = discover_parts(root)
    parsed = ParsedConfig(tenant_id=tenant_id, parts=parts)
    _parse_into(parsed, enumerate_objects(parts), progress_label)
    return parsed
