"""Multi-source ingest driver: turn Source adapters (ITS / artifacts) into :Document/:Artifact
owners + :Chunk, embed, link to config Objects. config_dump entries delegate to index/vectorize.
"""

from __future__ import annotations

from typing import Any

from . import chunking
from .config import Settings
from .embeddings.runtime import provider as get_provider
from .sources.base import Source, owner_fqn
from .sources.linking import link_mentions
from .sources.manifest import load_manifest
from .sources.registry import DOC_SOURCE_TYPES, build_source
from .storage import Neo4jStore
from .vectorizer import _embed_and_write


def _link_semantic(store, tenant_id, embedder, changed, top_k=3, min_score=0.45) -> int:
    """RELATES_TO: nearest config objects to each doc (opt-in recall). Best score per (doc, object)."""
    best: dict[tuple[str, str], float] = {}
    for doc_fqn, u in changed:
        vec = embedder.embed([f"{u.title}\n{u.text}"], is_query=True)[0]
        for h in store.vector_search(tenant_id, vec, top_k, index="chunk_embedding", source=["config"]):
            s = float(h.get("score") or 0.0)
            if h.get("fqn") and s >= min_score:
                key = (doc_fqn, h["fqn"])
                if s > best.get(key, 0.0):
                    best[key] = s
    rows = [{"doc_fqn": d, "object_fqn": o, "confidence": round(s, 4)} for (d, o), s in best.items()]
    return store.write_relates(tenant_id, rows) if rows else 0


def ingest_source(store: Neo4jStore, tenant_id: str, settings: Settings, src: Source, embedder,
                  reset: bool = False, link_semantic: bool = False) -> dict[str, Any]:
    """Ingest one doc corpus. Incremental by version_hash unless reset=True."""
    units = list(src.units())
    current = {owner_fqn(src.source, u.external_id): u for u in units}
    existing = store.doc_versions(tenant_id, src.source)

    if reset:
        store.delete_source(tenant_id, src.source)
        changed = list(current.items())
        deleted: list[str] = []
    else:
        changed = [(f, u) for f, u in current.items() if existing.get(f) != u.version_hash]
        deleted = [f for f in existing if f not in current]
        if changed or deleted:
            store.delete_docs(tenant_id, [f for f, _ in changed] + deleted)

    owner_rows, chunks = [], []
    for f, u in changed:
        props = {
            "source": src.source, "version_hash": u.version_hash, "title": u.title,
            "external_id": u.external_id, "section_path": u.section_path,
            "source_url": u.source_url, "config_id": ""}
        # adapter extras (platform_version, help_kind, name_norm, full_name_norm, ...) become
        # owner-node properties — drives version filtering and docinfo exact lookup.
        props.update({k: v for k, v in (u.extra or {}).items() if v is not None})
        owner_rows.append({"fqn": f, "props": props})
        chunks += chunking.doc_chunks(u.title, u.text, source=src.source, owner_fqn=f,
                                      section_path=u.section_path)

    if owner_rows:
        store.write_documents(tenant_id, src.owner_label, owner_rows)
    written, by_kind, stats = (0, {}, {})
    if chunks:
        written, by_kind, stats = _embed_and_write(
            store, tenant_id, embedder, chunks, owner_label=src.owner_label,
            label=f"ingest:{src.source}→{tenant_id}")
        store.create_vector_index(embedder.dim, name="chunk_embedding", prop="embedding")
        store.create_vector_index(embedder.dim, name="chunk_embedding_ident", prop="embedding_ident")
        store.create_fulltext_index()

    mentions = link_mentions(store, tenant_id, changed)
    relates = _link_semantic(store, tenant_id, embedder, changed) if link_semantic else 0
    return {"source": src.source, "owner_label": src.owner_label, "units": len(units),
            "changed": len(changed), "deleted": len(deleted), "chunks_written": written,
            "chunks_by_kind": by_kind, "mentions": mentions, "relates": relates, **stats}


def _ingest_config(entry: dict, tenant_id: str, settings: Settings, reset: bool) -> dict[str, Any]:
    """config_dump entry → existing rich pipeline (index + optional callgraph + vectorize)."""
    from .indexer import index_dump
    from .vectorizer import vectorize as run_vectorize

    out: dict[str, Any] = {"source": "config", "path": entry.get("path")}
    out["index"] = index_dump(entry["path"], tenant_id=tenant_id, settings=settings, reset=reset)
    if entry.get("callgraph", True):
        from .callgrapher import build_call_graph

        out["callgraph"] = build_call_graph(tenant_id, settings, reset=reset)
    out["vectorize"] = run_vectorize(tenant_id, settings, reset=reset, code=entry.get("code", True))
    return out


def ingest_manifest(manifest_path: str, settings: Settings, tenant_id: str | None = None,
                    only_type: str | None = None, reset: bool = False,
                    link_semantic: bool = False) -> dict[str, Any]:
    """Run all sources from a YAML/JSON manifest for a tenant."""
    data = load_manifest(manifest_path)
    tenant = tenant_id or data.get("tenant")
    if not tenant:
        raise ValueError("tenant not specified (manifest 'tenant' or --tenant-id)")

    results: list[dict[str, Any]] = []
    doc_entries: list[dict] = []
    for entry in data["sources"]:
        t = entry.get("type")
        if only_type and t != only_type:
            continue
        if t == "config_dump":
            results.append(_ingest_config(entry, tenant, settings, reset))
        elif t in DOC_SOURCE_TYPES:
            doc_entries.append(entry)
        else:
            raise ValueError(f"Unknown source type in manifest: {t!r}")

    if doc_entries:
        embedder = get_provider(settings)
        with Neo4jStore.from_settings(settings) as store:
            store.ensure_schema()
            for entry in doc_entries:
                results.append(
                    ingest_source(store, tenant, settings, build_source(entry), embedder,
                                  reset=reset, link_semantic=link_semantic)
                )
    return {"tenant_id": tenant, "results": results}
