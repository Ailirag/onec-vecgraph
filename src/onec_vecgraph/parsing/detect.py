"""Detect the source format of a dump/project root: 'configurator' (XML dump) or 'edt'."""

from __future__ import annotations

from pathlib import Path

from .edt.reader import is_edt_project


def detect_format(root: Path) -> str:
    """Configurator markers (Configuration.xml) win over EDT; checks root then its children."""
    root = Path(root)
    if (root / "Configuration.xml").is_file():
        return "configurator"
    if is_edt_project(root):
        return "edt"
    children = [p for p in root.iterdir() if p.is_dir()]
    if any((c / "Configuration.xml").is_file() for c in children):
        return "configurator"
    if any(is_edt_project(c) for c in children):
        return "edt"
    raise ValueError(
        f"Не удалось определить формат выгрузки (нет Configuration.xml и EDT-проекта): {root}"
    )
