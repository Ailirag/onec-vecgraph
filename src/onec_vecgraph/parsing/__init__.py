"""Parser for the 1C Configurator XML dump (metadata -> domain models)."""

from .dump import discover_parts, enumerate_objects, parse_config, parse_objects
from .model import ConfigPart, MetaObject, ParsedConfig

__all__ = [
    "discover_parts",
    "enumerate_objects",
    "parse_config",
    "parse_objects",
    "ConfigPart",
    "MetaObject",
    "ParsedConfig",
]
