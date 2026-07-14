"""onec-lite: zero-infrastructure MCP navigation over a live 1C working copy.

No Neo4j, no embeddings: the file system is the source of truth (Configurator XML dump
or 1C:EDT workspace), searches run through ripgrep with a pure-Python fallback, and code
answers are verified by the shared BSL parser. See `workspace`, `search`, `code_intel`,
`server`; started via `onec-vecgraph serve-lite`.
"""

from .workspace import LiteSource, Workspace

__all__ = ["LiteSource", "Workspace"]
