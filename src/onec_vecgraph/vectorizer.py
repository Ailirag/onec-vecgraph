"""Build metadata chunks from the graph, embed them (multi-vector), store as :Chunk nodes.

Supports full and incremental vectorization (re-embed only objects whose configVersion
changed since their chunks were built).
"""

from __future__ import annotations

from typing import Any, Iterator

from . import chunking
from .chunking import Chunk
from .config import Settings
from .embeddings.runtime import provider as get_runtime_provider
from .storage import Neo4jStore

_EMBED_BATCH = 512


def _free_gpu() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _read_bsl(path: str) -> str | None:
    from pathlib import Path

    try:
        return Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _iter_code_chunks(store: Neo4jStore, tenant_id: str, only: list[str] | None = None) -> Iterator[Chunk]:
    """Per-routine code chunks for object/common modules and form modules."""
    from .bsl.parser import parse_module
    from .parsing.forms import parse_form_handlers

    for m in store.read(
        "MATCH (o:Object {tenant_id: $t})-[:HAS_MODULE]->(mod:Module) "
        "WHERE ($only IS NULL OR o.fqn IN $only) "
        "RETURN o.fqn AS owner_fqn, o.kind AS owner_kind, o.name AS owner_name, o.synonym AS owner_syn, "
        "       o.config_version AS config_version, o.config_id AS config_id, mod.fqn AS module_fqn, "
        "       mod.module_type AS module_type, mod.path AS path",
        t=tenant_id, only=only,
    ):
        text = _read_bsl(m["path"])
        if not text:
            continue
        lines = text.split("\n")
        ctx = {**m, "handlers": {}}
        for rt in parse_module(text):
            yield from chunking.code_chunks(rt, "\n".join(lines[rt.start_line - 1 : rt.end_line]), ctx)

    for f in store.read(
        "MATCH (o:Object {tenant_id: $t})-[:HAS_FORM]->(frm:Form) "
        "WHERE frm.module_path IS NOT NULL AND ($only IS NULL OR o.fqn IN $only) "
        "RETURN o.fqn AS owner_fqn, o.kind AS owner_kind, o.name AS owner_name, o.synonym AS owner_syn, "
        "       o.config_version AS config_version, o.config_id AS config_id, frm.name AS form_name, "
        "       frm.fqn AS form_fqn, frm.module_path AS path, frm.form_path AS form_path",
        t=tenant_id, only=only,
    ):
        text = _read_bsl(f["path"])
        if not text:
            continue
        lines = text.split("\n")
        handlers = {h["handler"]: h for h in parse_form_handlers(f["form_path"])} if f.get("form_path") else {}
        ctx = {**f, "module_fqn": f["form_fqn"], "module_type": "FormModule", "handlers": handlers}
        for rt in parse_module(text):
            yield from chunking.code_chunks(rt, "\n".join(lines[rt.start_line - 1 : rt.end_line]), ctx)


def _iter_chunks(store: Neo4jStore, tenant_id: str, only: list[str] | None = None) -> Iterator[Chunk]:
    # Subsystems and Roles get dedicated, richer chunks below (composition / rights), so
    # they are excluded from the generic object card here.
    objects = store.read(
        "MATCH (o:Object {tenant_id: $t}) WHERE coalesce(o.stub, false) = false "
        "  AND NOT o.kind IN ['Subsystem', 'Role'] "
        "  AND ($only IS NULL OR o.fqn IN $only) "
        "OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE|HAS_DIMENSION|HAS_RESOURCE]->(f:Field) "
        "RETURN o.fqn AS fqn, o.kind AS kind, o.name AS name, o.synonym AS synonym, "
        "       o.comment AS comment, o.config_id AS config_id, o.config_version AS config_version, "
        "       collect({fqn: f.fqn, name: f.name, syn: f.synonym, role: f.role, "
        "                type: f.type_text, comment: f.comment}) AS fields",
        t=tenant_id, only=only,
    )
    for o in objects:
        yield chunking.object_chunk(o)
        yield from chunking.attribute_chunks(o)

    for row in store.read(
        "MATCH (o:Object {tenant_id: $t})-[:HAS_TABULAR_SECTION]->(ts:TabularSection)"
        "-[:HAS_ATTRIBUTE]->(f:Field) WHERE ($only IS NULL OR o.fqn IN $only) "
        "RETURN o.fqn AS owner_fqn, o.kind AS owner_kind, o.name AS owner_name, "
        "       o.synonym AS owner_syn, o.config_version AS config_version, ts.name AS ts_name, "
        "       ts.synonym AS ts_syn, f.fqn AS field_fqn, f.name AS field_name, "
        "       f.synonym AS field_syn, f.type_text AS type, o.config_id AS config_id",
        t=tenant_id, only=only,
    ):
        yield chunking.tabular_attribute_chunk(row)

    for row in store.read(
        "MATCH (o:Object {tenant_id: $t, kind: 'Enum'})-[:HAS_ENUM_VALUE]->(e:EnumValue) "
        "WHERE ($only IS NULL OR o.fqn IN $only) "
        "RETURN o.fqn AS enum_fqn, o.synonym AS enum_syn, o.name AS enum_name, "
        "       o.config_version AS config_version, e.fqn AS value_fqn, e.name AS value_name, "
        "       e.synonym AS value_syn, o.config_id AS config_id",
        t=tenant_id, only=only,
    ):
        yield chunking.enum_value_chunk(row)

    for row in store.read(
        "MATCH (o:Object {tenant_id: $t})-[:HAS_PREDEFINED]->(p:Predefined) "
        "WHERE ($only IS NULL OR o.fqn IN $only) "
        "RETURN o.fqn AS owner_fqn, o.kind AS owner_kind, o.name AS owner_name, "
        "       o.synonym AS owner_syn, o.config_version AS config_version, p.fqn AS pre_fqn, "
        "       p.name AS pre_name, p.description AS descr, o.config_id AS config_id",
        t=tenant_id, only=only,
    ):
        yield chunking.predefined_chunk(row)

    from .parsing.forms import extract_form_text

    for row in store.read(
        "MATCH (o:Object {tenant_id: $t})-[:HAS_FORM]->(f:Form) "
        "WHERE ($only IS NULL OR o.fqn IN $only) "
        "RETURN o.fqn AS owner_fqn, o.kind AS owner_kind, o.name AS owner_name, "
        "       o.synonym AS owner_syn, o.config_version AS config_version, f.fqn AS form_fqn, "
        "       f.name AS form_name, f.form_path AS form_path",
        t=tenant_id, only=only,
    ):
        row["form_text"] = extract_form_text(row["form_path"]) if row.get("form_path") else ""
        yield chunking.form_chunk(row)

    for row in store.read(
        "MATCH (s:Object {tenant_id: $t, kind: 'Subsystem'}) WHERE coalesce(s.stub, false) = false "
        "  AND ($only IS NULL OR s.fqn IN $only) "
        "OPTIONAL MATCH (s)-[:CONTAINS]->(m:Object) WHERE coalesce(m.stub, false) = false "
        "RETURN s.fqn AS fqn, s.name AS name, s.synonym AS synonym, s.comment AS comment, "
        "       s.config_id AS config_id, s.config_version AS config_version, "
        "       collect({name: m.name, syn: m.synonym, kind: m.kind}) AS members",
        t=tenant_id, only=only,
    ):
        yield chunking.subsystem_chunk(row)

    for row in store.read(
        "MATCH (r:Object {tenant_id: $t, kind: 'Role'}) WHERE coalesce(r.stub, false) = false "
        "  AND ($only IS NULL OR r.fqn IN $only) "
        "OPTIONAL MATCH (r)-[hr:HAS_RIGHT_ON]->(o:Object) "
        "RETURN r.fqn AS fqn, r.name AS name, r.synonym AS synonym, r.comment AS comment, "
        "       r.config_id AS config_id, r.config_version AS config_version, "
        "       collect({name: o.name, syn: o.synonym, granted: hr.granted}) AS rights",
        t=tenant_id, only=only,
    ):
        yield chunking.role_chunk(row)


def _embed_and_write(store: Neo4jStore, tenant_id: str, embedder, chunks: list[Chunk]) -> tuple[int, dict]:
    by_kind: dict[str, int] = {}
    written = 0
    for start in range(0, len(chunks), _EMBED_BATCH):
        batch = chunks[start : start + _EMBED_BATCH]
        sem = embedder.embed([c.text for c in batch], is_query=False)
        idt = embedder.embed([c.text_ident for c in batch], is_query=False)
        rows = [
            {"fqn": c.fqn, "owner_fqn": c.owner_fqn, "props": c.props(), "embedding": s, "embedding_ident": i}
            for c, s, i in zip(batch, sem, idt)
        ]
        written += store.write_chunks(tenant_id, rows)
        for c in batch:
            by_kind[c.chunk_kind] = by_kind.get(c.chunk_kind, 0) + 1
        _free_gpu()
    return written, by_kind


def vectorize(
    tenant_id: str, settings: Settings, reset: bool = True, incremental: bool = False, code: bool = False
) -> dict[str, Any]:
    embedder = get_runtime_provider(settings)
    with Neo4jStore.from_settings(settings) as store:
        store.ensure_schema()

        if incremental:
            stale = store.stale_chunk_owners(tenant_id)
            if stale:
                store.delete_chunks_for(tenant_id, stale)
            chunks = list(_iter_chunks(store, tenant_id, only=stale)) if stale else []
            if code and stale:
                chunks += list(_iter_code_chunks(store, tenant_id, only=stale))
            written, by_kind = _embed_and_write(store, tenant_id, embedder, chunks)
            store.create_vector_index(embedder.dim, name="chunk_embedding", prop="embedding")
            store.create_vector_index(embedder.dim, name="chunk_embedding_ident", prop="embedding_ident")
            store.create_fulltext_index()
            return {
                "mode": "incremental", "tenant_id": tenant_id, "model": settings.embedding_model,
                "device": getattr(embedder, "device", None), "dimensions": embedder.dim,
                "stale_objects": len(stale), "chunks_written": written, "chunks_by_kind": by_kind,
            }

        if reset:
            store.delete_chunks(tenant_id)
        chunks = list(_iter_chunks(store, tenant_id))
        if code:
            chunks += list(_iter_code_chunks(store, tenant_id))
        written, by_kind = _embed_and_write(store, tenant_id, embedder, chunks)
        store.create_vector_index(embedder.dim, name="chunk_embedding", prop="embedding")
        store.create_vector_index(embedder.dim, name="chunk_embedding_ident", prop="embedding_ident")
        store.create_fulltext_index()
        return {
            "mode": "full", "tenant_id": tenant_id, "model": settings.embedding_model,
            "provider": settings.embedding_provider, "device": getattr(embedder, "device", None),
            "dimensions": embedder.dim, "chunks_written": written, "chunks_by_kind": by_kind,
        }
