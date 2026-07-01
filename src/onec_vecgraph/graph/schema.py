"""Neo4j schema: uniqueness constraints and lookup indexes (idempotent)."""

from __future__ import annotations

NODE_LABELS = [
    "Object", "Field", "TabularSection", "EnumValue", "Predefined", "Form", "Module", "Chunk",
    "Routine", "Detail",
    # Multi-source doc corpora (ITS / project artifacts) — own the doc chunks, link to Objects.
    "Document", "Artifact",
    # Overlay deletions: a Tombstone (tenant_id, fqn) in an overlay tenant masks a baseline object.
    "Tombstone",
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
    # platform_docinfo exact lookup over platform/BSP help topics (Document owners).
    stmts.append(
        "CREATE INDEX document_name IF NOT EXISTS FOR (n:Document) ON (n.tenant_id, n.full_name_norm)"
    )
    stmts.append(
        "CREATE INDEX document_pv IF NOT EXISTS FOR (n:Document) ON (n.tenant_id, n.platform_version)"
    )
    # Classification facets for filtered search (owner-node): doc_topic / corpus_version.
    stmts.append(
        "CREATE INDEX document_topic IF NOT EXISTS FOR (n:Document) ON (n.tenant_id, n.doc_topic)"
    )
    stmts.append(
        "CREATE INDEX document_corpusv IF NOT EXISTS FOR (n:Document) ON (n.tenant_id, n.corpus_version)"
    )
    stmts.append(
        "CREATE INDEX artifact_topic IF NOT EXISTS FOR (n:Artifact) ON (n.tenant_id, n.doc_topic)"
    )
    stmts.append(
        "CREATE INDEX artifact_corpusv IF NOT EXISTS FOR (n:Artifact) ON (n.tenant_id, n.corpus_version)"
    )
    stmts.append(
        "CREATE INDEX object_corpusv IF NOT EXISTS FOR (n:Object) ON (n.tenant_id, n.corpus_version)"
    )
    # ── Runtime tenant provisioning (control-plane nodes; deliberately NOT in NODE_LABELS: no
    #    (tenant_id, fqn) constraint, and excluded from delete_tenant graph wipes). ──
    stmts.append(
        "CREATE CONSTRAINT tenant_key IF NOT EXISTS FOR (n:Tenant) REQUIRE n.tenant_id IS UNIQUE"
    )
    stmts.append(  # token_hash is GLOBALLY unique — a hash must never map to two tenants.
        "CREATE CONSTRAINT tenant_token_hash IF NOT EXISTS "
        "FOR (n:TenantToken) REQUIRE n.token_hash IS UNIQUE"
    )
    stmts.append(
        "CREATE INDEX tenant_token_tenant IF NOT EXISTS FOR (n:TenantToken) ON (n.tenant_id)"
    )
    return stmts
