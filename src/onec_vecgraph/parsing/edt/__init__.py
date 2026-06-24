"""1C:EDT format reader (.mdo metadata + EDT project layout) -> the shared domain model.

EDT is a different *serialization* of the same metadata model as the Configurator XML dump,
so this package only re-implements the parser; the domain model (parsing.model), graph
builder, vectorizer and call grapher stay format-agnostic.
"""

from .reader import discover_parts, enumerate_objects, parse_config, parse_objects

__all__ = ["discover_parts", "enumerate_objects", "parse_config", "parse_objects"]
