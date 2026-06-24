"""Parsers for 1C configurations -> shared domain models.

Two source formats are supported behind one API (see `dispatch`): the Configurator XML dump
(`dump`/`objects`) and 1C:EDT projects (`edt`). The format is auto-detected from the path.
"""

from .detect import detect_format
from .dispatch import discover_parts, enumerate_objects, parse_config, parse_objects
from .model import ConfigPart, MetaObject, ParsedConfig

__all__ = [
    "detect_format",
    "discover_parts",
    "enumerate_objects",
    "parse_config",
    "parse_objects",
    "ConfigPart",
    "MetaObject",
    "ParsedConfig",
]
