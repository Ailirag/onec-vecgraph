"""Overlay-tenant support: baseline (release) + per-task delta tenants.

The orchestrator ("Full development pipeline") computes a per-task delta from a developer's
XML dump and asks onec-vecgraph to (re)index only the touched objects into an ephemeral
overlay tenant ``<base>@task/<task_id>``, keyed alongside the baseline ``<base>`` tenant in the
same Neo4j. Search/graph merge is baseline ∪ overlay (overlay wins per fqn) minus tombstones.

This module holds the pure pieces (tenant naming + file→object-fqn mapping); the write driver
lives in :mod:`onec_vecgraph.overlay_index` and the write server in :mod:`onec_vecgraph.write_server`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .parsing import discover_parts, enumerate_objects
from .parsing.dump import TYPE_FOLDERS

OVERLAY_SEP = "@task/"

_ROOT_PREFIX = re.compile(r"^\d+/")  # delta object_key prefix = root index (config, then extensions)


def overlay_tenant_id(base_tenant_id: str, task_id: str) -> str:
    """Build the overlay tenant id for a task, e.g. ('acme@release', 'T-1') → 'acme@release@task/T-1'."""
    return f"{base_tenant_id}{OVERLAY_SEP}{task_id}"


def is_overlay_tenant(tenant_id: str) -> bool:
    return OVERLAY_SEP in tenant_id


def base_tenant_of(tenant_id: str) -> str:
    """The baseline tenant an overlay belongs to (identity for non-overlay tenants)."""
    return tenant_id.split(OVERLAY_SEP, 1)[0]


def task_of(tenant_id: str) -> str | None:
    return tenant_id.split(OVERLAY_SEP, 1)[1] if OVERLAY_SEP in tenant_id else None


def in_namespace(tenant_id: str, base_namespace: str) -> bool:
    """True if `tenant_id` is an overlay tenant under `base_namespace` (write-auth guard).

    A token authorized for base `<base>` may write only to `<base>@task/<anything>` — never to
    the baseline itself nor to another base's overlays.
    """
    return is_overlay_tenant(tenant_id) and base_tenant_of(tenant_id) == base_namespace


def fqn_from_object_key(key: str) -> str | None:
    """Derive an object fqn from a delta object-key, without touching the filesystem.

    Handles '0/Catalogs/Name.xml', '0/CommonModules/Name/Ext/Module.bsl' (module → owning object),
    and nested subsystems '0/Subsystems/A/Subsystems/B.xml' → 'Subsystem.A.Subsystem.B'. Used for
    deletions (the file is gone, so enumeration can't see it) and as a fallback for touched files.
    Returns None when the top folder is not a known metadata-object folder.
    """
    rel = _ROOT_PREFIX.sub("", key.replace("\\", "/")).strip("/")
    segs = [s for s in rel.split("/") if s]
    if not segs:
        return None
    if segs[0] == "Subsystems":
        names: list[str] = []
        i = 0
        while i + 1 < len(segs) and segs[i] == "Subsystems":
            name = segs[i + 1]
            names.append(name[:-4] if name.endswith(".xml") else name)
            i += 2
        return ".".join(f"Subsystem.{n}" for n in names) if names else None
    kind = TYPE_FOLDERS.get(segs[0])
    if not kind or len(segs) < 2:
        return None
    name = segs[1][:-4] if segs[1].endswith(".xml") else segs[1]
    return f"{kind}.{name}"


def _norm(p: str | Path) -> str:
    return Path(p).as_posix().lower()


def map_paths_to_object_fqns(roots: list[str], paths: list[str]) -> dict[str, str]:
    """Map dev-dump file paths (object XML or a BSL/form file under an object dir) to owning fqn.

    Uses the same folder→fqn enumeration as indexing, so keys align with the baseline. A path
    that equals an object's XML, or sits under its object directory (e.g. ``Ext/Module.bsl``,
    ``Forms/…``), resolves to that object's fqn. Unresolved paths are omitted from the result.
    """
    xml_to_fqn: dict[str, str] = {}
    dir_to_fqn: list[tuple[str, str]] = []
    for root in roots:
        parts = discover_parts(Path(root))
        for fqn, xml, object_dir, _cfg, _ver in enumerate_objects(parts):
            xml_to_fqn[_norm(xml)] = fqn
            dir_to_fqn.append((_norm(object_dir) + "/", fqn))
    # Longest object-dir prefix wins (nested subsystems / object inside another object's tree).
    dir_to_fqn.sort(key=lambda kv: len(kv[0]), reverse=True)

    out: dict[str, str] = {}
    for p in paths:
        np = _norm(p)
        if np in xml_to_fqn:
            out[p] = xml_to_fqn[np]
            continue
        for prefix, fqn in dir_to_fqn:
            if np.startswith(prefix):
                out[p] = fqn
                break
    return out
