"""Neo4j driver wrapper: connectivity, schema, batched graph writes, reads."""

from __future__ import annotations

from typing import Any

from neo4j import Driver, GraphDatabase

from ..config import Settings
from ..graph.builder import EdgeGroup, GraphData
from ..graph.schema import schema_statements

_BATCH = 1000

# Chunk owners — fixed allowlist so the label can be safely injected into Cypher (no param for labels).
_OWNER_LABELS = {"Object", "Document", "Artifact"}


class Neo4jStore:
    def __init__(self, driver: Driver, database: str) -> None:
        self._driver = driver
        self._database = database

    @classmethod
    def from_settings(cls, settings: Settings) -> "Neo4jStore":
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            # Quiet down "relationship type X does not exist yet" advisories etc.
            notifications_min_severity="OFF",
        )
        return cls(driver, settings.neo4j_database)

    # ── connectivity ──────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        self._driver.verify_connectivity()
        with self._driver.session(database=self._database) as session:
            comp = session.run(
                "CALL dbms.components() YIELD name, versions, edition "
                "RETURN name, versions, edition"
            ).single()
            nodes = session.run("MATCH (n) RETURN count(n) AS c").single()
        return {
            "connected": True,
            "database": self._database,
            "server": dict(comp) if comp else {},
            "node_count": nodes["c"] if nodes else 0,
        }

    # ── reads / writes ────────────────────────────────────────────────
    def read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        with self._driver.session(database=self._database) as session:
            return [record.data() for record in session.run(query, **params)]

    def write(self, query: str, **params: Any) -> None:
        with self._driver.session(database=self._database) as session:
            session.run(query, **params).consume()

    # ── schema ────────────────────────────────────────────────────────
    def ensure_schema(self) -> None:
        with self._driver.session(database=self._database) as session:
            for stmt in schema_statements():
                session.run(stmt).consume()

    # ── bulk graph load ───────────────────────────────────────────────
    def delete_tenant(self, tenant_id: str, batch: int = 25000) -> None:
        # Batched delete: a single DETACH DELETE of a large graph exceeds Neo4j's
        # per-transaction memory limit. Drop relationships first, then nodes, each
        # batch in its own auto-commit transaction.
        while True:
            rows = self.read(
                "MATCH (n {tenant_id: $tenant})-[r]->() WITH r LIMIT $batch "
                "DELETE r RETURN count(r) AS c",
                tenant=tenant_id, batch=batch,
            )
            if not rows or rows[0]["c"] == 0:
                break
        while True:
            rows = self.read(
                "MATCH (n {tenant_id: $tenant}) WITH n LIMIT $batch "
                "DELETE n RETURN count(n) AS c",
                tenant=tenant_id, batch=batch,
            )
            if not rows or rows[0]["c"] == 0:
                break

    def write_graph(self, graph: GraphData) -> dict[str, int]:
        tenant = graph.tenant_id
        nodes_written = 0
        for label, rows in graph.nodes.items():
            query = (
                f"UNWIND $rows AS r "
                f"MERGE (n:{label} {{tenant_id: r.tenant_id, fqn: r.fqn}}) "
                f"SET n += r"
            )
            for chunk in _chunks(rows):
                self.write(query, rows=chunk)
                nodes_written += len(chunk)

        edges_written = 0
        for group in graph.edge_groups():
            query = _edge_query(group)
            for chunk in _chunks(group.rows):
                self.write(query, rows=chunk, tenant=tenant)
                edges_written += len(chunk)

        return {"nodes": nodes_written, "edges": edges_written}

    def counts(self, tenant_id: str) -> dict[str, Any]:
        by_kind = self.read(
            "MATCH (o:Object {tenant_id: $t}) WHERE coalesce(o.stub, false) = false "
            "RETURN o.kind AS kind, count(*) AS n ORDER BY n DESC",
            t=tenant_id,
        )
        stubs = self.read(
            "MATCH (o:Object {tenant_id: $t}) WHERE o.stub = true RETURN count(o) AS n", t=tenant_id
        )
        real = self.read(
            "MATCH (o:Object {tenant_id: $t}) WHERE coalesce(o.stub, false) = false "
            "RETURN count(o) AS n",
            t=tenant_id,
        )
        by_label = {}
        for label in ["Object", "Field", "TabularSection", "EnumValue", "Predefined", "Form",
                      "Module", "Detail", "Document", "Artifact", "Chunk"]:
            rec = self.read(f"MATCH (n:{label} {{tenant_id: $t}}) RETURN count(n) AS n", t=tenant_id)
            by_label[label] = rec[0]["n"] if rec else 0
        rels = self.read(
            "MATCH (a {tenant_id: $t})-[r]->() RETURN type(r) AS rel, count(*) AS n "
            "ORDER BY n DESC",
            t=tenant_id,
        )
        return {
            "by_label": by_label,
            "real_objects": real[0]["n"] if real else 0,
            "stub_objects": stubs[0]["n"] if stubs else 0,
            "objects_by_kind": by_kind,
            "relationships": rels,
        }

    # ── vectorization (M2) ────────────────────────────────────────────
    def delete_chunks(self, tenant_id: str, batch: int = 25000) -> None:
        while True:
            rows = self.read(
                "MATCH (c:Chunk {tenant_id: $t}) WITH c LIMIT $batch DETACH DELETE c "
                "RETURN count(c) AS c",
                t=tenant_id, batch=batch,
            )
            if not rows or rows[0]["c"] == 0:
                break

    def write_chunks(self, tenant_id: str, rows: list[dict], owner_label: str = "Object") -> int:
        """Attach chunks to their owner via HAS_CHUNK. owner_label ∈ Object|Document|Artifact;
        the owner node must already exist (config Objects from indexing; doc owners from
        write_documents). Label is from a fixed allowlist (safe to inject)."""
        if owner_label not in _OWNER_LABELS:
            raise ValueError(f"Unsupported owner_label: {owner_label!r}")
        query = (
            "UNWIND $rows AS r "
            f"MATCH (o:{owner_label} {{tenant_id: $t, fqn: r.owner_fqn}}) "
            "MERGE (c:Chunk {tenant_id: $t, fqn: r.fqn}) "
            "SET c += r.props "
            "MERGE (o)-[:HAS_CHUNK]->(c) "
            "WITH c, r CALL db.create.setNodeVectorProperty(c, 'embedding', r.embedding) "
            "WITH c, r CALL db.create.setNodeVectorProperty(c, 'embedding_ident', r.embedding_ident)"
        )
        written = 0
        for chunk in _chunks(rows, 500):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    # ── doc corpora (multi-source: ITS / artifacts) ───────────────────
    def write_documents(self, tenant_id: str, owner_label: str, rows: list[dict]) -> int:
        """MERGE Document/Artifact owner nodes (rows: [{fqn, props}]). props carry source,
        version_hash, title, section_path, source_url, etc."""
        if owner_label not in _OWNER_LABELS or owner_label == "Object":
            raise ValueError(f"Unsupported doc owner_label: {owner_label!r}")
        query = (
            "UNWIND $rows AS r "
            f"MERGE (d:{owner_label} {{tenant_id: $t, fqn: r.fqn}}) SET d += r.props"
        )
        written = 0
        for chunk in _chunks(rows, 1000):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    def doc_versions(self, tenant_id: str, source: str) -> dict[str, str | None]:
        """{owner_fqn: version_hash} for a corpus — drives incremental re-ingest."""
        rows = self.read(
            "MATCH (d {tenant_id: $t}) WHERE (d:Document OR d:Artifact) AND d.source = $s "
            "RETURN d.fqn AS fqn, d.version_hash AS v",
            t=tenant_id, s=source,
        )
        return {r["fqn"]: r["v"] for r in rows}

    def delete_docs(self, tenant_id: str, fqns: list[str]) -> None:
        """Remove doc owners (and their chunks via HAS_CHUNK) by fqn — for incremental refresh."""
        for i in range(0, len(fqns), 500):
            self.write(
                "MATCH (d {tenant_id: $t}) WHERE (d:Document OR d:Artifact) AND d.fqn IN $f "
                "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) DETACH DELETE c, d",
                t=tenant_id, f=fqns[i : i + 500],
            )

    def existing_object_fqns(self, tenant_id: str, fqns: list[str]) -> set[str]:
        """Subset of `fqns` that exist as real (non-stub) Objects — for validating doc mentions."""
        rows = self.read(
            "MATCH (o:Object {tenant_id: $t}) WHERE o.fqn IN $f AND coalesce(o.stub, false) = false "
            "RETURN o.fqn AS fqn",
            t=tenant_id, f=fqns,
        )
        return {r["fqn"] for r in rows}

    def write_mentions(self, tenant_id: str, rows: list[dict]) -> int:
        """MENTIONS edges (doc owner → Object). rows: [{doc_fqn, object_fqn}]."""
        query = (
            "UNWIND $rows AS r "
            "MATCH (d {tenant_id: $t, fqn: r.doc_fqn}) WHERE d:Document OR d:Artifact "
            "MATCH (o:Object {tenant_id: $t, fqn: r.object_fqn}) "
            "MERGE (d)-[:MENTIONS]->(o)"
        )
        written = 0
        for chunk in _chunks(rows, 2000):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    def write_relates(self, tenant_id: str, rows: list[dict]) -> int:
        """RELATES_TO edges (doc owner → Object) with confidence. rows: [{doc_fqn, object_fqn, confidence}]."""
        query = (
            "UNWIND $rows AS r "
            "MATCH (d {tenant_id: $t, fqn: r.doc_fqn}) WHERE d:Document OR d:Artifact "
            "MATCH (o:Object {tenant_id: $t, fqn: r.object_fqn}) "
            "MERGE (d)-[rel:RELATES_TO]->(o) SET rel.confidence = r.confidence"
        )
        written = 0
        for chunk in _chunks(rows, 2000):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    def delete_source(self, tenant_id: str, source: str, batch: int = 5000) -> None:
        """Wipe an entire corpus (chunks + owners) for a tenant."""
        while True:
            rows = self.read(
                "MATCH (c:Chunk {tenant_id: $t, source: $s}) WITH c LIMIT $b DETACH DELETE c "
                "RETURN count(c) AS n",
                t=tenant_id, s=source, b=batch,
            )
            if not rows or rows[0]["n"] == 0:
                break
        while True:
            rows = self.read(
                "MATCH (d {tenant_id: $t}) WHERE (d:Document OR d:Artifact) AND d.source = $s "
                "WITH d LIMIT $b DETACH DELETE d RETURN count(d) AS n",
                t=tenant_id, s=source, b=batch,
            )
            if not rows or rows[0]["n"] == 0:
                break

    def create_vector_index(self, dim: int, name: str = "chunk_embedding",
                            prop: str = "embedding", similarity: str = "cosine") -> None:
        self.write(
            f"CREATE VECTOR INDEX {name} IF NOT EXISTS FOR (c:Chunk) ON (c.{prop}) "
            "OPTIONS {indexConfig: {`vector.dimensions`: $dim, `vector.similarity_function`: $sim}}",
            dim=dim, sim=similarity,
        )

    def create_fulltext_index(self, name: str = "chunk_text") -> None:
        # Index human text AND split-identifier tokens (c.text_tokens). Drop-recreate so the
        # field set is refreshed if it changed across versions (CREATE IF NOT EXISTS wouldn't).
        self.write(f"DROP INDEX {name} IF EXISTS")
        self.write(f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text, c.text_tokens]")

    # Shared chunk-search filter: by corpus source, owner kind, chunk kind, containing subsystem
    # (direct or via nested subsystems). `c` and `o` must be bound before this clause.
    _CHUNK_FILTER = (
        "  AND ($source IS NULL OR c.source IN $source) "
        "  AND ($chunk_kinds IS NULL OR c.chunk_kind IN $chunk_kinds) "
        "  AND ($kinds IS NULL OR o.kind IN $kinds) "
        "  AND ($subsystem IS NULL OR EXISTS { "
        "        MATCH (s:Object {tenant_id: $t, kind: 'Subsystem'}) "
        "        WHERE s.name = $subsystem OR s.fqn = $subsystem "
        "        MATCH (s)-[:HAS_SUBSYSTEM*0..]->(:Object)-[:CONTAINS]->(o) }) "
    )
    _CHUNK_RETURN = (
        "RETURN o.fqn AS fqn, o.kind AS kind, o.synonym AS synonym, c.fqn AS chunk_fqn, "
        "       c.name AS chunk_name, c.chunk_kind AS via, c.source AS source, c.text AS matched, score "
    )

    def vector_search(self, tenant_id: str, query_vec: list[float], fetch: int,
                      index: str = "chunk_embedding", kinds: list[str] | None = None,
                      chunk_kinds: list[str] | None = None, subsystem: str | None = None,
                      source: list[str] | None = None) -> list[dict[str, Any]]:
        return self.read(
            "CALL db.index.vector.queryNodes($index, $fetch, $vec) YIELD node AS c, score "
            "WHERE c.tenant_id = $t "
            "MATCH (o)-[:HAS_CHUNK]->(c) WHERE true "  # owner: Object | Document | Artifact
            + self._CHUNK_FILTER + self._CHUNK_RETURN + "ORDER BY score DESC",
            index=index, fetch=fetch, vec=query_vec, t=tenant_id,
            kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
        )

    def fulltext_search(self, tenant_id: str, query: str, limit: int,
                        index: str = "chunk_text", kinds: list[str] | None = None,
                        chunk_kinds: list[str] | None = None, subsystem: str | None = None,
                        source: list[str] | None = None) -> list[dict[str, Any]]:
        return self.read(
            "CALL db.index.fulltext.queryNodes($index, $q) YIELD node AS c, score "
            "WHERE c.tenant_id = $t "
            "MATCH (o)-[:HAS_CHUNK]->(c) WHERE true "  # owner: Object | Document | Artifact
            + self._CHUNK_FILTER + self._CHUNK_RETURN + "ORDER BY score DESC LIMIT $lim",
            index=index, q=query, t=tenant_id, lim=limit,
            kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
        )

    def filtered_chunk_count(self, tenant_id: str, cap: int, kinds: list[str] | None = None,
                             chunk_kinds: list[str] | None = None, subsystem: str | None = None,
                             source: list[str] | None = None) -> int:
        """Count chunks matching the filter, scanning at most `cap` (so the gate that decides
        exact-vs-index search is itself bounded). Returns `cap` when the set is at least that big."""
        rows = self.read(
            "MATCH (o)-[:HAS_CHUNK]->(c:Chunk {tenant_id: $t}) WHERE true "  # owner: Object|Document|Artifact
            + self._CHUNK_FILTER + "WITH c LIMIT $cap RETURN count(c) AS n",
            t=tenant_id, cap=cap, kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
        )
        return rows[0]["n"] if rows else 0

    def exact_vector_search(self, tenant_id: str, query_vec: list[float], fetch: int,
                            index: str = "chunk_embedding", kinds: list[str] | None = None,
                            chunk_kinds: list[str] | None = None, subsystem: str | None = None,
                            source: list[str] | None = None) -> list[dict[str, Any]]:
        """Exact cosine over the filtered candidate set (no vector-index recall loss). Use only
        when the filtered set is small — otherwise this scans too many vectors."""
        prop = "embedding_ident" if index.endswith("_ident") else "embedding"
        return self.read(
            "MATCH (o)-[:HAS_CHUNK]->(c:Chunk {tenant_id: $t}) WHERE true "  # owner: Object|Document|Artifact
            + self._CHUNK_FILTER
            + f"WITH o, c, vector.similarity.cosine(c.{prop}, $vec) AS score WHERE score IS NOT NULL "
            + self._CHUNK_RETURN + "ORDER BY score DESC LIMIT $fetch",
            t=tenant_id, vec=query_vec, fetch=fetch,
            kinds=kinds, chunk_kinds=chunk_kinds, subsystem=subsystem, source=source,
        )

    def chunk_count(self, tenant_id: str) -> int:
        rows = self.read("MATCH (c:Chunk {tenant_id: $t}) RETURN count(c) AS n", t=tenant_id)
        return rows[0]["n"] if rows else 0

    def stale_chunk_owners(self, tenant_id: str) -> list[str]:
        """Objects whose chunks are missing or built from a different configVersion."""
        rows = self.read(
            "MATCH (o:Object {tenant_id: $t}) WHERE coalesce(o.stub, false) = false "
            "OPTIONAL MATCH (o)-[:HAS_CHUNK]->(c:Chunk) "
            "WITH o, count(c) AS nchunks, collect(DISTINCT c.config_version) AS cvs "
            "WHERE nchunks = 0 OR NOT (o.config_version IN cvs) "
            "RETURN o.fqn AS fqn",
            t=tenant_id,
        )
        return [r["fqn"] for r in rows]

    def delete_chunks_for(self, tenant_id: str, fqns: list[str]) -> None:
        for i in range(0, len(fqns), 1000):
            self.write(
                "MATCH (o:Object {tenant_id: $t})-[:HAS_CHUNK]->(c:Chunk) "
                "WHERE o.fqn IN $f DETACH DELETE c",
                t=tenant_id, f=fqns[i : i + 1000],
            )

    # ── call graph (M3) ───────────────────────────────────────────────
    def delete_routines(self, tenant_id: str, batch: int = 25000) -> None:
        while True:
            rows = self.read(
                "MATCH (r:Routine {tenant_id: $t}) WITH r LIMIT $b DETACH DELETE r RETURN count(r) AS c",
                t=tenant_id, b=batch,
            )
            if not rows or rows[0]["c"] == 0:
                break

    def write_routines(self, tenant_id: str, rows: list[dict]) -> int:
        query = (
            "UNWIND $rows AS r "
            "MATCH (m:Module {tenant_id: $t, fqn: r.module_fqn}) "
            "MERGE (rt:Routine {tenant_id: $t, fqn: r.fqn}) SET rt += r.props "
            "MERGE (m)-[:DECLARES]->(rt)"
        )
        written = 0
        for chunk in _chunks(rows, 2000):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    def write_calls(self, tenant_id: str, rows: list[dict]) -> int:
        query = (
            "UNWIND $rows AS r "
            "MATCH (a:Routine {tenant_id: $t, fqn: r.src}) "
            "MATCH (b:Routine {tenant_id: $t, fqn: r.dst}) "
            "MERGE (a)-[c:CALLS]->(b) SET c.confidence = r.confidence, c.kind = r.kind"
        )
        written = 0
        for chunk in _chunks(rows, 2000):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    def routine_modules(self, tenant_id: str, only: list[str] | None = None) -> list[dict[str, Any]]:
        return self.read(
            "MATCH (o:Object {tenant_id: $t})-[:HAS_MODULE]->(m:Module) "
            "WHERE ($only IS NULL OR o.fqn IN $only) "
            "RETURN o.fqn AS obj_fqn, o.kind AS obj_kind, o.name AS obj_name, "
            "       o.config_version AS config_version, m.fqn AS module_fqn, "
            "       m.module_type AS mtype, m.path AS path",
            t=tenant_id, only=only,
        )

    def stale_routine_owners(self, tenant_id: str) -> list[tuple[str, str]]:
        """Objects with modules whose routines are missing or built from a different version."""
        rows = self.read(
            "MATCH (o:Object {tenant_id: $t})-[:HAS_MODULE]->(:Module) "
            "WITH DISTINCT o "
            "OPTIONAL MATCH (o)-[:HAS_MODULE]->(:Module)-[:DECLARES]->(rt:Routine) "
            "WITH o, count(rt) AS nr, collect(DISTINCT rt.config_version) AS cvs "
            "WHERE nr = 0 OR NOT (o.config_version IN cvs) "
            "RETURN o.fqn AS fqn, o.kind AS kind",
            t=tenant_id,
        )
        return [(r["fqn"], r["kind"]) for r in rows]

    def delete_routines_for(self, tenant_id: str, fqns: list[str]) -> None:
        for i in range(0, len(fqns), 500):
            self.write(
                "MATCH (o:Object {tenant_id: $t})-[:HAS_MODULE]->(:Module)-[:DECLARES]->(rt:Routine) "
                "WHERE o.fqn IN $f DETACH DELETE rt",
                t=tenant_id, f=fqns[i : i + 500],
            )

    def common_module_routine_index(self, tenant_id: str) -> dict[str, dict[str, str]]:
        rows = self.read(
            "MATCH (o:Object {tenant_id: $t, kind: 'CommonModule'})-[:HAS_MODULE]->(:Module)"
            "-[:DECLARES]->(rt:Routine) RETURN o.name AS module, rt.name AS method, rt.fqn AS fqn",
            t=tenant_id,
        )
        index: dict[str, dict[str, str]] = {}
        for r in rows:
            index.setdefault(r["module"], {})[r["method"]] = r["fqn"]
        return index

    def manager_module_routine_index(self, tenant_id: str) -> dict[str, dict[str, str]]:
        """Object name -> {method: routine fqn} for ManagerModule routines (for incremental
        re-resolution of Справочники.X.Метод() / Документы.X.Метод() calls)."""
        rows = self.read(
            "MATCH (o:Object {tenant_id: $t})-[:HAS_MODULE]->(m:Module {module_type: 'ManagerModule'})"
            "-[:DECLARES]->(rt:Routine) RETURN o.name AS object, rt.name AS method, rt.fqn AS fqn",
            t=tenant_id,
        )
        index: dict[str, dict[str, str]] = {}
        for r in rows:
            index.setdefault(r["object"], {})[r["method"]] = r["fqn"]
        return index

    def form_modules(self, tenant_id: str) -> list[dict[str, Any]]:
        return self.read(
            "MATCH (o:Object {tenant_id: $t})-[:HAS_FORM]->(f:Form) WHERE f.module_path IS NOT NULL "
            "RETURN o.fqn AS owner_fqn, o.kind AS owner_kind, o.name AS owner_name, "
            "       f.fqn AS form_fqn, f.module_path AS path, f.form_path AS form_path",
            t=tenant_id,
        )

    def write_form_routines(self, tenant_id: str, rows: list[dict]) -> int:
        query = (
            "UNWIND $rows AS r "
            "MATCH (f:Form {tenant_id: $t, fqn: r.form_fqn}) "
            "MERGE (rt:Routine {tenant_id: $t, fqn: r.fqn}) SET rt += r.props "
            "MERGE (f)-[:DECLARES]->(rt)"
        )
        written = 0
        for chunk in _chunks(rows, 2000):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    def write_handles(self, tenant_id: str, rows: list[dict]) -> int:
        query = (
            "UNWIND $rows AS r "
            "MATCH (f:Form {tenant_id: $t, fqn: r.form_fqn}) "
            "MATCH (rt:Routine {tenant_id: $t, fqn: r.routine_fqn}) "
            "MERGE (f)-[h:HANDLES]->(rt) SET h.event = r.event, h.element = r.element"
        )
        written = 0
        for chunk in _chunks(rows, 2000):
            self.write(query, t=tenant_id, rows=chunk)
            written += len(chunk)
        return written

    # ── incremental indexing (M5) ─────────────────────────────────────
    def object_versions(self, tenant_id: str) -> dict[str, str | None]:
        rows = self.read(
            "MATCH (o:Object {tenant_id: $t}) WHERE coalesce(o.stub, false) = false "
            "RETURN o.fqn AS fqn, o.config_version AS v",
            t=tenant_id,
        )
        return {r["fqn"]: r["v"] for r in rows}

    def scoped_delete_object(self, tenant_id: str, fqn: str) -> None:
        """Delete an object's owned children and its outgoing semantic edges, but KEEP the
        Object node, its incoming edges, and its Modules/Routines/Chunks (managed by the
        call-graph / vectorizer). Used before rebuilding a changed object."""
        self.write(
            "MATCH (o:Object {tenant_id: $t, fqn: $fqn}) "
            "OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE|HAS_DIMENSION|HAS_RESOURCE]->(f:Field) "
            "DETACH DELETE f "
            "WITH DISTINCT o "
            "OPTIONAL MATCH (o)-[:HAS_TABULAR_SECTION]->(ts:TabularSection) "
            "OPTIONAL MATCH (ts)-[:HAS_ATTRIBUTE]->(tf:Field) "
            "DETACH DELETE tf, ts "
            "WITH DISTINCT o "
            "OPTIONAL MATCH (o)-[:HAS_ENUM_VALUE]->(ev:EnumValue) "
            "OPTIONAL MATCH (o)-[:HAS_PREDEFINED]->(pd:Predefined) "
            "OPTIONAL MATCH (o)-[:HAS_FORM]->(fm:Form) "
            "OPTIONAL MATCH (o)-[:HAS_DETAIL]->(d:Detail) "
            "DETACH DELETE ev, pd, fm, d "
            "WITH DISTINCT o "
            "OPTIONAL MATCH (o)-[r:OWNED_BY|CONTAINS|HAS_SUBSYSTEM|SUBSCRIBES|HANDLED_BY|HAS_RIGHT_ON|WRITES_TO]->() "
            "DELETE r",
            t=tenant_id, fqn=fqn,
        )

    def delete_object_full(self, tenant_id: str, fqn: str) -> None:
        """Fully remove an object that no longer exists in the dump (node + all it owns)."""
        self.write(
            "MATCH (o:Object {tenant_id: $t, fqn: $fqn}) "
            "OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE|HAS_DIMENSION|HAS_RESOURCE|HAS_TABULAR_SECTION|"
            "HAS_ENUM_VALUE|HAS_PREDEFINED|HAS_FORM|HAS_MODULE|HAS_CHUNK|HAS_DETAIL]->(c) "
            "OPTIONAL MATCH (c)-[:HAS_ATTRIBUTE|DECLARES]->(gc) "
            "DETACH DELETE gc, c, o",
            t=tenant_id, fqn=fqn,
        )

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "Neo4jStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _chunks(rows: list[dict], size: int = _BATCH):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _edge_query(group: EdgeGroup) -> str:
    if group.soft:
        return (
            f"UNWIND $rows AS r "
            f"MATCH (a:{group.src_label} {{tenant_id: $tenant, fqn: r.src}}) "
            f"MERGE (b:Object {{tenant_id: $tenant, fqn: r.dst}}) "
            f"  ON CREATE SET b.kind = r.dst_kind, b.name = r.dst_name, b.stub = true "
            f"MERGE (a)-[rel:{group.rel}]->(b) "
            f"SET rel += r.props"
        )
    return (
        f"UNWIND $rows AS r "
        f"MATCH (a:{group.src_label} {{tenant_id: $tenant, fqn: r.src}}) "
        f"MATCH (b:{group.dst_label} {{tenant_id: $tenant, fqn: r.dst}}) "
        f"MERGE (a)-[rel:{group.rel}]->(b) "
        f"SET rel += r.props"
    )
