"""Format-agnostic parser API: route discover/enumerate/parse to the configurator or EDT reader.

`indexer.py` imports these names from `onec_vecgraph.parsing` and stays format-unaware.
Root-taking calls (`discover_parts`, `parse_config`) detect the format from the path;
parts-taking calls (`enumerate_objects`, `parse_objects`) route on `ConfigPart.fmt`.
"""

from __future__ import annotations

from pathlib import Path

from . import dump as _configurator
from .detect import detect_format
from .edt import reader as _edt
from .model import ConfigPart, ParsedConfig

_READERS = {"configurator": _configurator, "edt": _edt}


def _reader_for_root(root: Path):
    return _READERS[detect_format(Path(root))]


def _reader_for_parts(parts: list[ConfigPart]):
    fmt = parts[0].fmt if parts else "configurator"
    return _READERS.get(fmt, _configurator)


def discover_parts(root: str | Path) -> list[ConfigPart]:
    return _reader_for_root(root).discover_parts(Path(root))


def enumerate_objects(parts: list[ConfigPart]):
    return _reader_for_parts(parts).enumerate_objects(parts)


def parse_config(root: str | Path, tenant_id: str, progress_label: str | None = None) -> ParsedConfig:
    return _reader_for_root(root).parse_config(Path(root), tenant_id, progress_label)


def parse_objects(tenant_id: str, parts: list[ConfigPart], refs,
                  progress_label: str | None = None) -> ParsedConfig:
    return _reader_for_parts(parts).parse_objects(tenant_id, parts, refs, progress_label)
