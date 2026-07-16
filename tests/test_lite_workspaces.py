"""Named workspaces: state v2 + миграция, порядок резолва, изоляция репозиториев,
workspace-параметр инструментов, admin upsert/activate/delete, FTS на воркспейс.

Ключевой кейс — ДВА репозитория с одинаковым именем конфигурации («ТестБаза»):
до переноса name_index в Workspace глобальный кэш code_intel вернул бы индекс чужого
репо, и вызов резолвился бы в модуль другой рабочей копии."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onec_vecgraph.lite import admin as lite_admin
from onec_vecgraph.lite import fts as lite_fts
from onec_vecgraph.lite import launcher
from onec_vecgraph.lite import platform_help as ph
from onec_vecgraph.lite import server as lite_server

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_BASE_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-0000000000cc">
  <name>ТестБаза</name>
</mdclass:Configuration>
"""

_CATALOG_TPL = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {ns} uuid="11111111-0000-0000-0000-0000000000{uid}">
  <name>{name}</name>
</mdclass:Catalog>
"""

_COMMON_TPL = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:CommonModule {ns} uuid="22222222-0000-0000-0000-0000000000{uid}">
  <name>Общий</name>
  <server>true</server>
</mdclass:CommonModule>
"""

_DOC_TPL = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Document {ns} uuid="33333333-0000-0000-0000-0000000000{uid}">
  <name>Заказ</name>
</mdclass:Document>
"""


def _mk(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(root: Path, catalog: str, common_routine: str, uid: str) -> Path:
    """Мини-EDT репо: конфигурация «ТестБаза», свой справочник, Общий.<routine>,
    Документ.Заказ, чей модуль вызывает Общий.МетодА()."""
    src = root / "conf" / "src"
    _mk(src / "Configuration" / "Configuration.mdo", _BASE_CONFIG)
    _mk(src / "Catalogs" / catalog / f"{catalog}.mdo",
        _CATALOG_TPL.format(ns=_MDCLASS, name=catalog, uid=uid))
    _mk(src / "CommonModules" / "Общий" / "Общий.mdo",
        _COMMON_TPL.format(ns=_MDCLASS, uid=uid))
    _mk(src / "CommonModules" / "Общий" / "Module.bsl",
        f"Функция {common_routine}() Экспорт\n    Возврат Истина;\nКонецФункции\n")
    _mk(src / "Documents" / "Заказ" / "Заказ.mdo", _DOC_TPL.format(ns=_MDCLASS, uid=uid))
    _mk(src / "Documents" / "Заказ" / "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\n"
        "    Общий.МетодА();\nКонецПроцедуры\n")
    return root


@pytest.fixture()
def two_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state" / "config.json"))
    for var in ("ONEC_LITE_WORKSPACE", "ONEC_LITE_ROOT", "ONEC_LITE_EXT_ROOTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(lite_server, "_WORKSPACES", {})
    monkeypatch.setattr(lite_server, "_HELP", ph.HelpCatalog())
    monkeypatch.setattr(lite_server, "_HELP_INIT", True)
    a = _repo(tmp_path / "repo_a", "А_Товары", "МетодА", "aa")
    b = _repo(tmp_path / "repo_b", "Б_Партнеры", "МетодБ", "bb")
    return a, b


# --------------------------------------------------------------------------- #
# State v2
# --------------------------------------------------------------------------- #

def test_v1_state_migrates_to_default_workspace(tmp_path: Path) -> None:
    f = tmp_path / "cfg.json"
    f.write_text(json.dumps({"root": "H:\\ut", "ext_roots": ["D:\\ext"],
                             "rg_path": "C:\\rg.exe"}), encoding="utf-8")
    wss, active = lite_admin.load_workspaces(f)
    assert wss == {"default": {"root": "H:\\ut", "ext_roots": ["D:\\ext"]}}
    assert active == "default"
    # legacy-шимы работают поверх v2
    assert lite_admin.load_paths(f) == ("H:\\ut", ["D:\\ext"])


def test_upsert_activate_delete_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "cfg.json"
    lite_admin.upsert_workspace(f, "ut", "H:\\ut", [])
    lite_admin.upsert_workspace(f, "erp", "D:\\erp", ["D:\\erp_ext"])
    wss, active = lite_admin.load_workspaces(f)
    assert set(wss) == {"ut", "erp"} and active == "ut"  # первый стал активным
    assert lite_admin.set_active(f, "erp") is True
    assert lite_admin.load_workspaces(f)[1] == "erp"
    assert lite_admin.set_active(f, "нет") is False
    assert lite_admin.delete_workspace(f, "ut") is True
    wss, active = lite_admin.load_workspaces(f)
    assert set(wss) == {"erp"} and active == "erp"
    assert lite_admin.load_state(f)["version"] == 2


def test_ws_name_validation() -> None:
    assert lite_admin.normalize_ws_name(" ut-1.х ") == "ut-1.х"
    assert lite_admin.normalize_ws_name("плохое имя") is None
    assert lite_admin.normalize_ws_name("a/b") is None
    assert lite_admin.normalize_ws_name("") is None


# --------------------------------------------------------------------------- #
# Резолв воркспейса
# --------------------------------------------------------------------------- #

def test_resolve_order_arg_env_active(two_repos: tuple[Path, Path],
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = two_repos
    state = lite_admin.state_file()
    lite_admin.upsert_workspace(state, "a", str(a), [])
    lite_admin.upsert_workspace(state, "b", str(b), [])
    lite_admin.set_active(state, "a")
    assert str(lite_server._ws().root) == str(a)  # noqa: SLF001 - active
    monkeypatch.setenv("ONEC_LITE_WORKSPACE", "b")  # env сильнее active
    assert str(lite_server._ws().root) == str(b)  # noqa: SLF001
    assert str(lite_server._ws("a").root) == str(a)  # noqa: SLF001 - аргумент сильнее env
    assert lite_server.default_workspace_name() == "b"


def test_unknown_workspace_lists_known(two_repos: tuple[Path, Path]) -> None:
    a, _b = two_repos
    lite_admin.upsert_workspace(lite_admin.state_file(), "a", str(a), [])
    with pytest.raises(RuntimeError, match="Известные: a"):
        lite_server._ws("nope")  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Инструменты с workspace-параметром + изоляция одноимённых конфигураций
# --------------------------------------------------------------------------- #

def test_tools_isolated_per_workspace(two_repos: tuple[Path, Path]) -> None:
    a, b = two_repos
    state = lite_admin.state_file()
    lite_admin.upsert_workspace(state, "a", str(a), [])
    lite_admin.upsert_workspace(state, "b", str(b), [])

    names_a = [o["name"] for o in lite_server.list_objects("Catalog", workspace="a")["objects"]]
    names_b = [o["name"] for o in lite_server.list_objects("Catalog", workspace="b")["objects"]]
    assert names_a == ["А_Товары"] and names_b == ["Б_Партнеры"]
    assert lite_server.overview(workspace="b")["workspace"] == "b"

    # Оба репо называются «ТестБаза», у обоих есть CommonModule «Общий» — но резолв
    # вызова Общий.МетодА() обязан остаться внутри СВОЕГО воркспейса.
    res_a = lite_server.find_callees("Document", "Заказ", "ОбработкаПроведения",
                                     module="Object", workspace="a")
    assert [c["kind"] for c in res_a["resolved"]] == ["common_module"]
    res_b = lite_server.find_callees("Document", "Заказ", "ОбработкаПроведения",
                                     module="Object", workspace="b")
    assert res_b["resolved"] == []  # в репо B у «Общий» нет МетодА — must not утечь из A
    assert any("МетодА" in c["call"] for c in res_b["unresolved"])


def test_list_workspaces_tool(two_repos: tuple[Path, Path],
                              monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = two_repos
    state = lite_admin.state_file()
    lite_admin.upsert_workspace(state, "a", str(a), [])
    lite_admin.upsert_workspace(state, "b", str(b), [])
    monkeypatch.setenv("ONEC_LITE_WORKSPACE", "b")
    lite_server._ws("a")  # noqa: SLF001 - прогреем один
    res = lite_server.list_workspaces()
    rows = {w["name"]: w for w in res["workspaces"]}
    assert set(rows) == {"a", "b"}
    assert rows["a"]["active"] is True and rows["a"]["loaded"] is True
    assert res["default_workspace"] == "b"


# --------------------------------------------------------------------------- #
# Админка: upsert по имени, activate/delete, снапшот
# --------------------------------------------------------------------------- #

def test_apply_named_workspace_and_snapshot(two_repos: tuple[Path, Path]) -> None:
    a, b = two_repos
    snap, err = lite_server.apply_admin_paths(str(a), "", name="a")
    assert err is None and snap["workspace"] == "a" and snap["configured"]
    snap2, err2 = lite_server.apply_admin_paths(str(b), "", name="b")
    assert err2 is None and snap2["workspace"] == "b"
    names = [w["name"] for w in snap2["workspaces"]]
    assert names == ["a", "b"]
    wss, active = lite_admin.load_workspaces(lite_admin.state_file())
    assert set(wss) == {"a", "b"} and active == "a"  # activate — явное действие
    _snap3, err3 = lite_server.apply_admin_paths(str(b), "", name="плохое имя")
    assert err3 is not None and "Недопустимое имя" in err3


def test_fts_per_workspace(two_repos: tuple[Path, Path]) -> None:
    if not lite_fts.fts_available():
        pytest.skip("FTS5 недоступен в этой сборке sqlite3")
    a, b = two_repos
    state = lite_admin.state_file()
    lite_admin.upsert_workspace(state, "a", str(a), [])
    lite_admin.upsert_workspace(state, "b", str(b), [])
    ws_a = lite_server._ws("a")  # noqa: SLF001
    ws_b = lite_server._ws("b")  # noqa: SLF001
    assert lite_fts.db_path_for(ws_a) != lite_fts.db_path_for(ws_b)
    assert "error" not in lite_fts.index_for(ws_a).build()
    assert "error" not in lite_fts.index_for(ws_b).build()
    hits_a = lite_server.fts_search("МетодА", workspace="a")
    hits_b = lite_server.fts_search("МетодА", workspace="b")
    assert any(r["title"] == "МетодА" for r in hits_a["results"])
    assert all(r["title"] != "МетодА" for r in hits_b["results"])


def test_launcher_workspace_flag(two_repos: tuple[Path, Path],
                                 monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture) -> None:
    a, _b = two_repos
    rc = launcher.main(["check", "--root", str(a), "--workspace", "утка"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Воркспейс: утка" in out and "ТестБаза" in out
    import os

    assert os.environ["ONEC_LITE_WORKSPACE"] == "утка"
