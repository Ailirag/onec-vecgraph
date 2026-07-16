"""onec-lite launcher: режимы stdio/admin/check, маппинг аргументов в env, ленивый снапшот."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from onec_vecgraph.lite import launcher
from onec_vecgraph.lite import platform_help as ph
from onec_vecgraph.lite import server as lite_server

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_BASE_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-0000000000bb">
  <name>ТестБаза</name>
</mdclass:Configuration>
"""

_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-0000-0000-0000-0000000000bb">
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
    """launcher пишет env напрямую — delenv через monkeypatch откатывает это после теста."""
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state.json"))
    for var in ("ONEC_LITE_ROOT", "ONEC_LITE_EXT_ROOTS", "ONEC_LITE_HELP",
                "ONEC_LITE_ADMIN", "ONEC_LITE_HOST", "ONEC_LITE_PORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ONEC_LITE_WORKSPACE", raising=False)
    monkeypatch.setattr(lite_server, "_WORKSPACES", {})
    monkeypatch.setattr(lite_server, "_HELP", ph.HelpCatalog())
    monkeypatch.setattr(lite_server, "_HELP_INIT", True)


def test_check_prints_sources_and_sets_env(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _mini_edt(tmp_path / "ws")
    rc = launcher.main(["check", "--root", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Рабочая копия" in out and "ТестБаза" in out and "база" in out
    assert os.environ["ONEC_LITE_ROOT"] == str(root)


def test_check_unconfigured_hints_admin(capsys: pytest.CaptureFixture) -> None:
    rc = launcher.main(["check"])
    out = capsys.readouterr().out
    assert rc == 1 and "onec-lite admin" in out


def test_stdio_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lite_server, "run", calls.append)
    assert launcher.main([]) == 0
    assert calls == ["stdio"]
    assert "ONEC_LITE_ADMIN" not in os.environ


def test_admin_mode_env_url_and_browser(monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture) -> None:
    calls: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(lite_server, "run", calls.append)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    class _InstantTimer:
        def __init__(self, _delay, fn, args=()):
            self._fn, self._args = fn, args

        def start(self):
            self._fn(*self._args)

    monkeypatch.setattr(launcher.threading, "Timer", _InstantTimer)
    assert launcher.main(["admin", "--port", "8123", "--host", "127.0.0.1"]) == 0
    out = capsys.readouterr().out
    assert calls == ["streamable-http"]
    assert os.environ["ONEC_LITE_ADMIN"] == "true"
    assert os.environ["ONEC_LITE_PORT"] == "8123"
    assert opened == ["http://127.0.0.1:8123/admin"]
    assert "8123/admin" in out and "8123/mcp" in out

    calls.clear()
    opened.clear()
    assert launcher.main(["admin", "--no-browser", "--port", "8124"]) == 0
    assert calls == ["streamable-http"] and opened == []


def test_ext_and_help_args_become_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lite_server, "run", lambda _t: None)
    launcher.main(["stdio", "--ext-root", "D:\\ext1", "--ext-root", "D:\\ext2",
                   "--help-path", "8.3.27=C:\\bin"])
    assert os.environ["ONEC_LITE_EXT_ROOTS"] == "D:\\ext1;D:\\ext2"
    assert os.environ["ONEC_LITE_HELP"] == "8.3.27=C:\\bin"


def test_snapshot_lazily_configures_from_env(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    root = _mini_edt(tmp_path / "ws")
    monkeypatch.setenv("ONEC_LITE_ROOT", str(root))
    snap = lite_server._snapshot()  # noqa: SLF001 - страница админки при запуске с --root
    assert snap["configured"] is True and snap["root"] == str(root)
