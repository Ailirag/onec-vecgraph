"""Neo4j schema: uniqueness constraints and lookup indexes (idempotent)."""

from __future__ import annotations

NODE_LABELS = [
    "Object", "Field", "TabularSection", "EnumValue", "Predefined", "Form", "Module", "Chunk",
    "Routine", "Detail",
]


def schema_statements() -> list[str]:
    stmts: list[str] = []
    for label in NODE_LABELS:
        lower = label.lower()
        stmts.append(
            f"CREATE CONSTRAINT {lower}_key IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE (n.tenant_id, n.fqn) IS UNIQUE"
        )
    stmts.append(
        "CREATE INDEX object_kind IF NOT EXISTS FOR (n:Object) ON (n.tenant_id, n.kind)"
    )
    stmts.append(
        "CREATE INDEX object_name IF NOT EXISTS FOR (n:Object) ON (n.tenant_id, n.name)"
    )
    return stmts
