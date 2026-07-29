from onec_vecgraph.bsl.parser import Call, Routine
from onec_vecgraph.callgrapher import _override_edge, _resolve


def _rt(name, calls):
    rt = Routine(name=name, kind="Procedure", export=False, start_line=1, end_line=2)
    rt.calls = calls
    return rt


def test_override_edge_targets_base_routine_in_qualified_module() -> None:
    mf = "Catalog.Контрагенты.Module.ObjectModule@ext:ДИТ"
    rt = Routine(name="Расш_ПередЗаписью", kind="Procedure", export=False, start_line=1, end_line=2,
                 override_mode="Вместо", override_target="ПередЗаписью")
    props = {}
    rows = _override_edge(f"{mf}::{rt.name}", mf, rt, props)
    assert props["override_mode"] == "Вместо"  # also recorded on the routine node
    assert rows == [{
        "src": "Catalog.Контрагенты.Module.ObjectModule@ext:ДИТ::Расш_ПередЗаписью",
        "dst": "Catalog.Контрагенты.Module.ObjectModule::ПередЗаписью",  # base config, @ext stripped
        "mode": "Вместо", "target_name": "ПередЗаписью",
    }]


def test_override_edge_skipped_for_base_module_and_plain_routines() -> None:
    base_mf = "Catalog.Контрагенты.Module.ObjectModule"
    over = Routine(name="X", kind="Procedure", export=False, start_line=1, end_line=2,
                   override_mode="Вместо", override_target="ПередЗаписью")
    # override annotation but NOT in an @ext module → not an extension override, skip
    assert _override_edge(f"{base_mf}::X", base_mf, over, {}) == []
    plain = Routine(name="Y", kind="Procedure", export=False, start_line=1, end_line=2)
    assert _override_edge(f"{base_mf}@ext:Z::Y", f"{base_mf}@ext:Z", plain, {}) == []


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


# ── build stamps: an object that declares nothing must stop looking "stale" ──────
#
# Regression for the loop that turned every nightly refresh into a full rebuild: staleness used to
# mean "this object has no routines", and EDT omits Module.bsl for an empty common module, so such
# an object was stale forever. Being a CommonModule it then tripped the correctness fallback and the
# whole tenant was rebuilt — 27 minutes of work per no-op run on the sandbox.

class _FakeStore:
    """The slice of Neo4jStore that build_call_graph touches, recording what it was asked to do."""

    def __init__(self, *, stale=(), modules=(), forms=()):
        self.stale = list(stale)
        self._modules, self._forms = list(modules), list(forms)
        self.ops: list[str] = []  # ordered log of graph mutations
        self.stamped_modules: list[dict] = []
        self.stamped_forms: list[dict] = []
        self.written_routines: list[dict] = []
        self.written_form_routines: list[dict] = []
        self.pruned: list[str] = []
        self.edges_cleared: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ensure_schema(self) -> None:
        pass

    def stale_routine_owners(self, t):
        return self.stale

    def routine_modules(self, t, only=None):
        return [m for m in self._modules if only is None or m["obj_fqn"] in only]

    def form_modules(self, t, only=None):
        return [f for f in self._forms if only is None or f["owner_fqn"] in only]

    def common_module_routine_index(self, t):
        return {}

    def manager_module_routine_index(self, t):
        return {}

    def delete_routines(self, t):
        self.ops.append("delete_all_routines")

    def clear_graph_built(self, t):
        self.ops.append("clear_stamps")

    def write_routines(self, t, rows):
        self.ops.append("write_routines")
        self.written_routines = rows
        return len(rows)

    def write_form_routines(self, t, rows):
        self.ops.append("write_form_routines")
        self.written_form_routines = rows
        return len(rows)

    def prune_outdated_routines(self, t, fqns):
        self.ops.append("prune")
        self.pruned = list(fqns)
        return 0

    def clear_owned_edges_for(self, t, fqns):
        self.ops.append("clear_owned_edges")
        self.edges_cleared = list(fqns)

    def write_calls(self, t, rows):
        return len(rows)

    def write_handles(self, t, rows):
        return len(rows)

    def write_overrides(self, t, rows):
        return len(rows)

    def mark_modules_built(self, t, rows):
        self.ops.append("stamp_modules")
        self.stamped_modules = rows
        return len(rows)

    def mark_forms_built(self, t, rows):
        self.ops.append("stamp_forms")
        self.stamped_forms = rows
        return len(rows)


def _use(monkeypatch, store):
    import onec_vecgraph.callgrapher as cg

    class _Factory:
        @staticmethod
        def from_settings(settings):
            return store

    monkeypatch.setattr(cg, "Neo4jStore", _Factory)


def _module(obj_fqn, path, *, kind="Catalog", version="v2", mtype="ObjectModule"):
    name = obj_fqn.split(".", 1)[-1]
    return {"obj_fqn": obj_fqn, "obj_kind": kind, "obj_name": name, "config_version": version,
            "module_fqn": f"{obj_fqn}.Module.{mtype}", "mtype": mtype, "path": str(path)}


def _bsl(tmp_path, name, body="Процедура Тест()\nКонецПроцедуры\n"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_incremental_stamps_module_whose_file_is_absent(monkeypatch, tmp_path) -> None:
    """The core fix: a module with no .bsl on disk yields no routines, yet must still be stamped
    at the version it was processed at — otherwise it comes back stale on every later run."""
    from onec_vecgraph.callgrapher import build_call_graph
    from onec_vecgraph.config import Settings

    absent = _module("Catalog.Товары", tmp_path / "нет-такого.bsl")
    store = _FakeStore(stale=[("Catalog.Товары", "Catalog")], modules=[absent])
    _use(monkeypatch, store)

    res = build_call_graph("t1", Settings(), reset=False, incremental=True)

    assert res["files_missing"] == 1 and res["routines"] == 0
    assert store.stamped_modules == [
        {"fqn": "Catalog.Товары.Module.ObjectModule", "version": "v2"}]


def test_incremental_reparses_forms_and_carries_version(monkeypatch, tmp_path) -> None:
    """Forms were never re-parsed incrementally. Harmless while every run fell back to full — a
    silent staleness bug the moment the incremental path starts being taken."""
    from onec_vecgraph.callgrapher import build_call_graph
    from onec_vecgraph.config import Settings

    form_bsl = _bsl(tmp_path, "ФормаМодуль.bsl", "Процедура ПриОткрытии(Отказ)\nКонецПроцедуры\n")
    form = {"owner_fqn": "Catalog.Товары", "owner_kind": "Catalog", "owner_name": "Товары",
            "config_version": "v2", "form_fqn": "Catalog.Товары.Form.Форма",
            "path": str(form_bsl), "form_path": None}
    store = _FakeStore(stale=[("Catalog.Товары", "Catalog")],
                       modules=[_module("Catalog.Товары", _bsl(tmp_path, "М.bsl"))],
                       forms=[form])
    _use(monkeypatch, store)

    res = build_call_graph("t1", Settings(), reset=False, incremental=True)

    assert res["form_modules_parsed"] == 1 and res["form_routines"] == 1
    assert store.stamped_forms == [{"fqn": "Catalog.Товары.Form.Форма", "version": "v2"}]
    # the version has to reach the routine props, or pruning by version deletes it straight back
    assert store.written_form_routines[0]["props"]["config_version"] == "v2"


def test_incremental_writes_before_pruning_and_never_wipes_routines(monkeypatch, tmp_path) -> None:
    """Routines are re-MERGEd first and pruned by version afterwards, so a surviving routine keeps
    its node — and with it the incoming CALLS from unchanged callers this pass cannot re-resolve."""
    from onec_vecgraph.callgrapher import build_call_graph
    from onec_vecgraph.config import Settings

    store = _FakeStore(stale=[("Catalog.Товары", "Catalog")],
                       modules=[_module("Catalog.Товары", _bsl(tmp_path, "М.bsl"))])
    _use(monkeypatch, store)

    build_call_graph("t1", Settings(), reset=False, incremental=True)

    assert "delete_all_routines" not in store.ops
    assert store.ops.index("write_routines") < store.ops.index("prune")
    assert store.ops.index("prune") < store.ops.index("stamp_modules")
    assert store.edges_cleared == ["Catalog.Товары"]  # owned edges re-resolved from scratch


def test_full_clears_stamps_before_deleting_and_stamps_last(monkeypatch, tmp_path) -> None:
    """Crash-safety: stamps come off before the routines do and go back on only once the graph is
    whole, so an interrupted rebuild reads as stale instead of as fresh-but-empty."""
    from onec_vecgraph.callgrapher import build_call_graph
    from onec_vecgraph.config import Settings

    store = _FakeStore(modules=[_module("Catalog.Товары", _bsl(tmp_path, "М.bsl")),
                                _module("CommonModule.Пустой", tmp_path / "нет.bsl",
                                        kind="CommonModule", mtype="Module")])
    _use(monkeypatch, store)

    build_call_graph("t1", Settings(), reset=True, incremental=False)

    assert store.ops.index("clear_stamps") < store.ops.index("delete_all_routines")
    assert store.ops.index("delete_all_routines") < store.ops.index("write_routines")
    assert store.ops[-2:] == ["stamp_modules", "stamp_forms"]
    # both modules stamped, including the one whose file is missing
    assert [r["fqn"] for r in store.stamped_modules] == [
        "Catalog.Товары.Module.ObjectModule", "CommonModule.Пустой.Module.Module"]


def test_stale_common_module_still_forces_full_rebuild(monkeypatch, tmp_path) -> None:
    """The fallback stays: CALLS from unchanged callers into a changed common module must be
    re-resolved, and only a full pass re-parses those callers."""
    from onec_vecgraph.callgrapher import build_call_graph
    from onec_vecgraph.config import Settings

    store = _FakeStore(stale=[("CommonModule.Сервис", "CommonModule")],
                       modules=[_module("CommonModule.Сервис", _bsl(tmp_path, "М.bsl"),
                                        kind="CommonModule", mtype="Module")])
    _use(monkeypatch, store)

    res = build_call_graph("t1", Settings(), reset=False, incremental=True)

    assert res["mode"] == "full" and res["fallback_reason"] == "changed CommonModule(s)"
    assert "delete_all_routines" in store.ops


def test_nothing_stale_does_no_work(monkeypatch) -> None:
    """The steady state a correct stamp makes reachable: nothing changed, nothing touched."""
    from onec_vecgraph.callgrapher import build_call_graph
    from onec_vecgraph.config import Settings

    store = _FakeStore(stale=[])
    _use(monkeypatch, store)

    res = build_call_graph("t1", Settings(), reset=False, incremental=True)

    assert res == {"mode": "incremental", "tenant_id": "t1", "stale_objects": 0,
                   "routines": 0, "calls_written": 0}
    assert store.ops == []
