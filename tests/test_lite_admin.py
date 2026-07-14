"""Lite web admin: path parsing/persistence, live re-point of the workspace, rendering.

HTTP-маршруты тонкие (см. lite/server.py) — тестируем чистые хелперы и apply-логику,
по образцу тестов baseline-дашборда."""

from __future__ import annotations

from pathlib import Path

import pytest

from onec_vecgraph.lite import admin as lite_admin
from onec_vecgraph.lite import search as lite_search
from onec_vecgraph.lite import server as lite_server

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_BASE_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-0000000000aa">
  <name>ТестБаза</name>
</mdclass:Configuration>
"""

_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-0000-0000-0000-0000000000aa">
  <name>Тест</name>
</mdclass:Catalog>
"""


def _mini_edt(root: Path) -> Path:
    src = root / "conf" / "src"
    (src / "Configuration").mkdir(parents=True)
    (src / "Configuration" / "Configuration.mdo").write_text(_BASE_CONFIG, encoding="utf-8")
    (src / "Catalogs" / "Тест").mkdir(parents=True)
    (src / "Catalogs" / "Тест" / "Тест.mdo").write_text(_CATALOG, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Чистые env/state/workspace на каждый тест: админка не должна трогать ~/.onec-lite."""
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state" / "config.json"))
    monkeypatch.delenv("ONEC_LITE_ROOT", raising=False)
    monkeypatch.delenv("ONEC_LITE_EXT_ROOTS", raising=False)
    monkeypatch.setattr(lite_server, "_WS", None)
    monkeypatch.setattr(lite_server, "_RG_INIT", False)
    monkeypatch.setattr(lite_search, "_RG_OVERRIDE", None)


def test_parse_ext_roots_lines_semicolons_quotes() -> None:
    text = ' "D:\\ext\\Один" ;\nD:\\ext\\Два\n\n;  \n D:\\ext\\Три '
    assert lite_admin.parse_ext_roots(text) == ["D:\\ext\\Один", "D:\\ext\\Два", "D:\\ext\\Три"]
    assert lite_admin.parse_ext_roots("") == []


def test_state_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "cfg.json"
    assert lite_admin.load_paths(f) is None
    lite_admin.save_paths(f, "H:\\ut", ["D:\\ext"])
    assert lite_admin.load_paths(f) == ("H:\\ut", ["D:\\ext"])
    f.write_text("{broken", encoding="utf-8")
    assert lite_admin.load_paths(f) is None


def test_apply_admin_paths_configures_and_persists(tmp_path: Path) -> None:
    root = _mini_edt(tmp_path / "ws")
    snap, err = lite_server.apply_admin_paths(str(root), "")
    assert err is None and snap is not None
    assert snap["configured"] and snap["root"] == str(root)
    assert [s["source"] for s in snap["sources"]] == ["ТестБаза"]
    assert snap["sources"][0]["objects"] == 1
    saved = lite_admin.load_paths(lite_admin.state_file())
    assert saved == (str(root), [])
    assert lite_server._WS is not None  # noqa: SLF001


def test_apply_bad_path_keeps_previous_workspace(tmp_path: Path) -> None:
    root = _mini_edt(tmp_path / "ws")
    lite_server.apply_admin_paths(str(root), "")
    old = lite_server._WS  # noqa: SLF001
    # частичный успех: ошибка рабочей копии репортится, но прежний воркспейс жив,
    # а битый путь НЕ попадает в сохранённое состояние
    snap, err = lite_server.apply_admin_paths(str(tmp_path / "нет_такого"), "")
    assert err is not None and "Рабочая копия" in err
    assert snap is not None and snap["configured"] and snap["root"] == str(root)
    assert lite_server._WS is old  # noqa: SLF001
    assert lite_admin.load_paths(lite_admin.state_file()) == (str(root), [])
    _snap2, err2 = lite_server.apply_admin_paths("", "")
    assert err2 is not None


def test_ws_falls_back_to_saved_state(tmp_path: Path) -> None:
    root = _mini_edt(tmp_path / "ws")
    lite_admin.save_paths(lite_admin.state_file(), str(root), [])
    ws = lite_server._ws()  # noqa: SLF001
    assert str(ws.root) == str(root)


def test_ws_unconfigured_raises() -> None:
    with pytest.raises(RuntimeError, match="не сконфигурирован"):
        lite_server._ws()  # noqa: SLF001


def test_admin_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEC_LITE_ADMIN", raising=False)
    assert lite_server._admin_enabled() is False  # noqa: SLF001
    monkeypatch.setenv("ONEC_LITE_ADMIN", "true")
    assert lite_server._admin_enabled() is True  # noqa: SLF001
    monkeypatch.setenv("ONEC_LITE_ADMIN", "0")
    assert lite_server._admin_enabled() is False  # noqa: SLF001


def test_rg_override_set_clear_and_validation(tmp_path: Path) -> None:
    fake = tmp_path / "rg.exe"
    fake.write_text("")
    assert lite_search.set_rg_path(str(fake)) == str(fake)
    assert lite_search.rg_path() == str(fake) and lite_search.rg_override() == str(fake)
    # несуществующий override -> rg_path() честно None (не тихий фолбэк на автопоиск)
    lite_search.set_rg_path(str(tmp_path / "нет.exe"))
    assert lite_search.rg_path() is None
    lite_search.set_rg_path(None)
    assert lite_search.rg_override() is None
    assert lite_search.rg_path() != str(fake)  # вернулись к автопоиску


def test_apply_admin_rg_persists_and_survives_restart(tmp_path: Path) -> None:
    fake = tmp_path / "rg.exe"
    fake.write_text("")
    snap, err = lite_server.apply_admin_paths("", "", "", rg_text=str(fake))
    assert err is None and snap is not None
    assert snap["rg_override"] == str(fake) and snap["rg"] == str(fake)
    assert lite_admin.load_state(lite_admin.state_file())["rg_path"] == str(fake)
    # битый путь -> ошибка, прежний override не тронут
    _s2, err2 = lite_server.apply_admin_paths("", "", "", rg_text=str(tmp_path / "нет.exe"))
    assert err2 is not None and "ripgrep" in err2
    assert lite_search.rg_override() == str(fake)
    # «рестарт»: новый процесс подхватывает сохранённый путь из state
    lite_search.set_rg_path(None)
    lite_server._RG_INIT = False  # noqa: SLF001
    lite_server._init_rg_from_state()  # noqa: SLF001
    assert lite_search.rg_override() == str(fake)
    # пустое поле = вернуться к автопоиску (и это персистится)
    _s3, err3 = lite_server.apply_admin_paths("", "", "", rg_text="")
    assert err3 is None and lite_search.rg_override() is None
    assert lite_admin.load_state(lite_admin.state_file())["rg_path"] == ""


def test_render_page_shows_state_and_escapes(tmp_path: Path) -> None:
    root = _mini_edt(tmp_path / "ws")
    snap, _ = lite_server.apply_admin_paths(str(root), "")
    html = lite_admin.render_admin_page(
        snap, rg=None, state_path="C:\\state.json", error='<script>alert(1)</script>'
    )
    assert "ТестБаза" in html and "настроен" in html
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Python-фолбэке" in html  # rg=None -> честное предупреждение
    empty = lite_admin.render_admin_page(
        lite_admin.workspace_snapshot(None), rg="C:\\rg.exe", state_path="s.json"
    )
    assert "не настроен" in empty and "Источники не загружены" in empty
