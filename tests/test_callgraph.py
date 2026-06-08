from onec_vecgraph.bsl.parser import Call, Routine
from onec_vecgraph.callgrapher import _resolve


def _rt(name, calls):
    rt = Routine(name=name, kind="Procedure", export=False, start_line=1, end_line=2)
    rt.calls = calls
    return rt


def test_resolve_manager_call_medium_confidence() -> None:
    mf = "Catalog.Контрагенты.Module.ObjectModule"
    parsed = [(mf, [_rt("Тест", [Call(qualifier="Контрагенты", method="СоздатьЭлемент")])])]
    manager_index = {"Контрагенты": {"СоздатьЭлемент": "Catalog.Контрагенты.Module.ManagerModule::СоздатьЭлемент"}}
    rows, stats = _resolve(parsed, local_index={}, common_index={}, manager_index=manager_index)
    assert stats["calls_resolved_manager"] == 1
    assert rows[0]["kind"] == "manager" and rows[0]["confidence"] == "medium"
    assert rows[0]["dst"] == "Catalog.Контрагенты.Module.ManagerModule::СоздатьЭлемент"


def test_resolve_prefers_common_over_manager_and_counts_unresolved() -> None:
    mf = "CommonModule.X.Module.Module"
    parsed = [(mf, [_rt("Тест", [
        Call(qualifier="СервисА", method="Сделать"),       # common module -> high
        Call(qualifier="Контрагенты", method="НетТакого"),  # manager miss -> unresolved
    ])])]
    common_index = {"СервисА": {"Сделать": "CommonModule.СервисА.Module.Module::Сделать"}}
    manager_index = {"Контрагенты": {"СоздатьЭлемент": "..."}}
    rows, stats = _resolve(parsed, {}, common_index, manager_index)
    assert stats["calls_resolved_common_module"] == 1
    assert stats["calls_resolved_manager"] == 0
    assert stats["calls_unresolved"] == 1
    assert {r["kind"] for r in rows} == {"common_module"}
