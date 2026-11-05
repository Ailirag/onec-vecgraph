"""onec-vecgraph: 1C:Enterprise configuration vectorization + dependency/call graph over Neo4j (MCP)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("onec-vecgraph")
except PackageNotFoundError:
    __version__ = "0.2.1"
