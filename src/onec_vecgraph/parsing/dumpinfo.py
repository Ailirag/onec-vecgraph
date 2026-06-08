"""Parse ConfigDumpInfo.xml -> per-object configVersion hashes (for incremental indexing)."""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from .ns import DUMPINFO, q


def parse_dump_info(part_dir: Path) -> dict[str, str]:
    """Return {object_fqn: configVersion} for the top-level objects of a part.

    Only direct children of <ConfigVersions> carry the object-level hash (which rolls up
    the object's own definition incl. attributes). Forms/child metadata are nested.
    """
    path = part_dir / "ConfigDumpInfo.xml"
    if not path.is_file():
        return {}
    root = etree.parse(str(path)).getroot()
    versions = root.find(q(DUMPINFO, "ConfigVersions"))
    if versions is None:
        return {}
    out: dict[str, str] = {}
    for md in versions.findall(q(DUMPINFO, "Metadata")):  # direct children = top-level objects
        name = md.get("name")
        ver = md.get("configVersion")
        if name and ver:
            out[name] = ver
    return out
