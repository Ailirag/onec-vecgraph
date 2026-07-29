"""Live check of the call-graph staleness Cypher against a real Neo4j.

Why a script and not a test: the staleness rule lives inside Cypher strings, and the suite has no
Neo4j — the unit tests can only assert what Python does around the queries. The first version of
this rule shipped a Cypher operator-precedence bug (`AND` binds tighter than `OR`, so "any Module,
or a stale form" made every object stale forever) that passed every unit test. Run this after
touching stale_routine_owners / mark_*_built / prune_outdated_routines / clear_owned_edges_for.

    python scripts/probe_callgraph_stamps.py          # uses the ambient NEO4J_* settings

Everything happens in the throwaway tenant below and is deleted again on the way out, so it is
safe to point at a production instance.
"""

from __future__ import annotations

import sys

from onec_vecgraph.config import Settings
from onec_vecgraph.storage.neo4j_store import Neo4jStore

TENANT = "__cgstamp_probe__"

_failures = 0


def check(label: str, got, expected) -> None:
    global _failures
    good = got == expected
    if not good:
        _failures += 1
    print(f"  {label:<58} {'OK' if good else 'ПРОВАЛ':<6} получено={got}")


def main() -> int:
    with Neo4jStore.from_settings(Settings()) as st:
        st.write("MATCH (n {tenant_id:$t}) DETACH DELETE n", t=TENANT)
        st.write(
            "CREATE (o:Object {tenant_id:$t, fqn:'Catalog.Тест', kind:'Catalog', name:'Тест', "
            "                  config_version:'v1'}) "
            "CREATE (m:Module {tenant_id:$t, fqn:'Catalog.Тест.Module.ObjectModule', "
            "                  module_type:'ObjectModule', path:'/нет.bsl'}) "
            "CREATE (f:Form {tenant_id:$t, fqn:'Catalog.Тест.Form.Ф', module_path:'/нет2.bsl'}) "
            "CREATE (o)-[:HAS_MODULE]->(m) CREATE (o)-[:HAS_FORM]->(f)", t=TENANT)

        print("── критерий устаревания ──")
        check("без отметок объект устаревший",
              st.stale_routine_owners(TENANT), [("Catalog.Тест", "Catalog")])

        st.mark_modules_built(TENANT, [{"fqn": "Catalog.Тест.Module.ObjectModule", "version": "v1"}])
        check("модуль отмечен, форма нет — всё ещё устаревший",
              st.stale_routine_owners(TENANT), [("Catalog.Тест", "Catalog")])

        st.mark_forms_built(TENANT, [{"fqn": "Catalog.Тест.Form.Ф", "version": "v1"}])
        check("ГЛАВНОЕ: отмечен, рутин НОЛЬ — не устаревший", st.stale_routine_owners(TENANT), [])

        st.write("MATCH (o:Object {tenant_id:$t}) SET o.config_version = 'v2'", t=TENANT)
        check("версия сменилась — снова устаревший",
              st.stale_routine_owners(TENANT), [("Catalog.Тест", "Catalog")])

        print("── рутины: prune по версии и сохранение входящих рёбер ──")
        st.write(
            "MATCH (m:Module {tenant_id:$t, fqn:'Catalog.Тест.Module.ObjectModule'}) "
            "CREATE (keep:Routine {tenant_id:$t, fqn:'Catalog.Тест.Module.ObjectModule::Живая', "
            "                      config_version:'v2'}) "
            "CREATE (gone:Routine {tenant_id:$t, fqn:'Catalog.Тест.Module.ObjectModule::Исчезла', "
            "                      config_version:'v1'}) "
            "CREATE (m)-[:DECLARES]->(keep) CREATE (m)-[:DECLARES]->(gone) "
            "CREATE (caller:Routine {tenant_id:$t, fqn:'Внешний::Вызывающая', config_version:'v9'}) "
            "CREATE (caller)-[:CALLS]->(keep) "   # входящее от неизменившегося — должно уцелеть
            "CREATE (keep)-[:CALLS]->(caller)",   # исходящее — пересобирается, должно уйти
            t=TENANT)

        check("prune удалил ровно одну устаревшую рутину",
              st.prune_outdated_routines(TENANT, ["Catalog.Тест"]), 1)
        left = [r["fqn"] for r in st.read(
            "MATCH (rt:Routine {tenant_id:$t}) RETURN rt.fqn AS fqn ORDER BY fqn", t=TENANT)]
        check("уцелели живая и внешняя вызывающая", left,
              ["Catalog.Тест.Module.ObjectModule::Живая", "Внешний::Вызывающая"])

        st.clear_owned_edges_for(TENANT, ["Catalog.Тест"])
        edges = [r["e"] for r in st.read(
            "MATCH (a:Routine {tenant_id:$t})-[:CALLS]->(b:Routine {tenant_id:$t}) "
            "RETURN a.fqn + ' -> ' + b.fqn AS e", t=TENANT)]
        check("исходящее снято, входящее от неизменившегося цело", edges,
              ["Внешний::Вызывающая -> Catalog.Тест.Module.ObjectModule::Живая"])

        print("── снятие отметок (защита от обрыва полной пересборки) ──")
        st.clear_graph_built(TENANT)
        check("после clear_graph_built объект снова устаревший",
              st.stale_routine_owners(TENANT), [("Catalog.Тест", "Catalog")])

        st.write("MATCH (n {tenant_id:$t}) DETACH DELETE n", t=TENANT)
        check("фиктивный тенант убран",
              st.read("MATCH (n {tenant_id:$t}) RETURN count(n) AS c", t=TENANT)[0]["c"], 0)

    print("\nИТОГ:", "ВСЕ ПРОВЕРКИ ПРОШЛИ" if not _failures else f"ПРОВАЛОВ: {_failures}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
