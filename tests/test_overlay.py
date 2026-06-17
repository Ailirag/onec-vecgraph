from onec_vecgraph.overlay import (
    base_tenant_of,
    fqn_from_object_key,
    in_namespace,
    is_overlay_tenant,
    overlay_tenant_id,
    task_of,
)


def test_overlay_tenant_id_roundtrip() -> None:
    t = overlay_tenant_id("grand-dev-mdm@release", "TASK-MDM-0001")
    assert t == "grand-dev-mdm@release@task/TASK-MDM-0001"
    assert is_overlay_tenant(t)
    assert base_tenant_of(t) == "grand-dev-mdm@release"
    assert task_of(t) == "TASK-MDM-0001"


def test_baseline_is_not_overlay() -> None:
    base = "grand-dev-mdm@release"
    assert not is_overlay_tenant(base)
    assert base_tenant_of(base) == base       # identity for non-overlay
    assert task_of(base) is None


def test_in_namespace_write_guard() -> None:
    base = "grand-dev-mdm@release"
    # an overlay under the authorized base → allowed
    assert in_namespace("grand-dev-mdm@release@task/T1", base)
    # writing the baseline itself → denied (overlay write must target @task/*)
    assert not in_namespace(base, base)
    # another base's overlay → denied (no cross-base writes)
    assert not in_namespace("other@release@task/T1", base)


def test_fqn_from_object_key_top_level() -> None:
    assert fqn_from_object_key("0/Catalogs/OldObject.xml") == "Catalog.OldObject"
    assert fqn_from_object_key("1/Documents/РеализацияТоваров.xml") == "Document.РеализацияТоваров"


def test_fqn_from_object_key_module_maps_to_owning_object() -> None:
    assert fqn_from_object_key("0/CommonModules/ОбщегоНазначения/Ext/Module.bsl") == "CommonModule.ОбщегоНазначения"


def test_fqn_from_object_key_nested_subsystem() -> None:
    assert fqn_from_object_key("0/Subsystems/Продажи/Subsystems/Опт.xml") == "Subsystem.Продажи.Subsystem.Опт"
    assert fqn_from_object_key("0/Subsystems/Продажи.xml") == "Subsystem.Продажи"


def test_fqn_from_object_key_unknown_folder() -> None:
    assert fqn_from_object_key("0/WeirdFolder/X.xml") is None
    assert fqn_from_object_key("0/") is None


def test_write_auth_token_map_parses_base_namespace() -> None:
    from onec_vecgraph.config import Settings

    s = Settings(write_auth_tokens="wtok=grand-dev-mdm@release, other=acme@release")
    m = s.write_auth_token_map()
    assert m["wtok"] == "grand-dev-mdm@release"
    assert m["other"] == "acme@release"


def test_merge_edge_rows_overlay_wins_and_masks() -> None:
    from onec_vecgraph.queries import _merge_edge_rows

    # Incoming edges: row['fqn'] is the SOURCE object that owns the edge.
    base = [{"fqn": "A"}, {"fqn": "B"}, {"fqn": "D"}]
    overlay = [{"fqn": "A"}]                       # A is touched → its edge is rewritten in overlay
    merged = _merge_edge_rows(base, overlay, src_key="fqn", touched={"A"}, tombstoned={"D"})
    by = {r["fqn"]: r for r in merged}
    assert by["A"]["layer"] == "working"          # overlay version wins
    assert by["B"]["layer"] == "release"          # unchanged baseline source kept
    assert "D" not in by                          # tombstoned source dropped


def test_tag_layer_drops_tombstoned_targets() -> None:
    from onec_vecgraph.queries import _tag_layer

    out = _tag_layer([{"fqn": "X"}, {"fqn": "Y"}], "working", {"Y"})
    assert out == [{"fqn": "X", "layer": "working"}]


def test_merge_edge_rows_object_ownership() -> None:
    # callers context: row['object'] owns the edge, row['fqn'] is the caller routine.
    from onec_vecgraph.queries import _merge_edge_rows

    base = [{"fqn": "M1::a", "object": "CommonModule.M1"}, {"fqn": "M2::b", "object": "CommonModule.M2"}]
    overlay = [{"fqn": "M1::a2", "object": "CommonModule.M1"}]   # M1 touched → its callers rewritten
    merged = _merge_edge_rows(base, overlay, src_key="object", touched={"CommonModule.M1"}, tombstoned=set())
    by = {r["fqn"]: r for r in merged}
    assert by["M1::a2"]["layer"] == "working"   # overlay caller from the touched object
    assert by["M2::b"]["layer"] == "release"    # unchanged baseline caller kept
    assert "M1::a" not in by                    # baseline caller of touched object superseded
