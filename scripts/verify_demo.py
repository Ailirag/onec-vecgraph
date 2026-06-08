"""Ad-hoc verification of the new role-facing capabilities against tenant `demo`.
Writes results to scripts/verify_demo.out.json (read via the Read tool — console mangles Cyrillic)."""
import json
import sys

from onec_vecgraph import queries
from onec_vecgraph.config import get_settings
from onec_vecgraph.embeddings.runtime import provider
from onec_vecgraph.storage import Neo4jStore

T = sys.argv[1] if len(sys.argv) > 1 else "demo"
s = get_settings()
emb = provider(s)
out = {}

with Neo4jStore.from_settings(s) as store:
    out["metrics"] = queries.metrics(store, T)

    # 1. chunk_kinds filter = code only -> all hits routine-grained
    r = queries.hybrid_search(store, T, "получить ответ от модели", emb, top_k=5, chunk_kinds=["code"])
    out["filter_code_only"] = [{"fqn": x["fqn"], "via": x["via"], "routine_fqn": x.get("routine_fqn")} for x in r["results"]]

    # 2. kinds filter = Subsystem -> subsystem chunks
    r = queries.semantic_search(store, T, "подсистема работы с нейросетями", emb, top_k=5, kinds=["Subsystem"])
    out["filter_subsystem_kind"] = [{"fqn": x["fqn"], "kind": x["kind"], "via": x["via"]} for x in r["results"]]

    # 3. identifier sub-word tokenization (hybrid uses fulltext over text_tokens)
    r = queries.hybrid_search(store, T, "Провайдеры", emb, top_k=5)
    out["ident_subword"] = [{"fqn": x["fqn"], "via": x["via"], "sources": x.get("sources")} for x in r["results"]]

    # 4. GraphRAG expand
    r = queries.hybrid_search(store, T, "модель нейросети", emb, top_k=3, expand=True)
    out["expanded"] = [{"fqn": x["fqn"], "via": x["via"], "context": x.get("context")} for x in r["results"]]

    # 5. role chunk search
    r = queries.semantic_search(store, T, "права доступа роль", emb, top_k=5, chunk_kinds=["role"])
    out["role_chunks"] = [{"fqn": x["fqn"], "via": x["via"]} for x in r["results"]]

    # 6. entry points present on routines?
    out["entry_point_counts"] = store.read(
        "MATCH (rt:Routine {tenant_id:$t}) WHERE rt.entry_point IS NOT NULL "
        "RETURN rt.entry_point AS ep, count(*) AS n ORDER BY n DESC", t=T)

    # 7. find_handlers on the first object that has any entry-point routine
    row = store.read(
        "MATCH (o:Object {tenant_id:$t})-[:HAS_MODULE]->(:Module)-[:DECLARES]->(rt:Routine) "
        "WHERE rt.entry_point IS NOT NULL RETURN o.fqn AS fqn LIMIT 1", t=T)
    if row:
        out["find_handlers_example"] = queries.find_handlers(store, T, row[0]["fqn"])

    # 8. WRITES_TO edges (documents -> registers)
    out["writes_to"] = store.read(
        "MATCH (d:Object {tenant_id:$t})-[:WRITES_TO]->(r:Object) "
        "RETURN d.fqn AS document, r.fqn AS register LIMIT 20", t=T)

    # 9. manager-resolved calls sample
    out["manager_calls"] = store.read(
        "MATCH (a:Routine {tenant_id:$t})-[c:CALLS {kind:'manager'}]->(b:Routine) "
        "RETURN a.fqn AS caller, b.fqn AS callee LIMIT 10", t=T)

path = f"scripts/verify_{T}.out.json"
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("written", path)
