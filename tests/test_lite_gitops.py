"""Workspace updates from remote: guards, fetch/ff-pull, owned mirrors, admin/CLI wiring.

Все сценарии — на локальном bare-«remote» (git init --bare в tmp): сеть не нужна,
но проходит настоящий системный git — тот же путь, что и в бою."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from onec_vecgraph.lite import admin as lite_admin
from onec_vecgraph.lite import gitops, launcher
from onec_vecgraph.lite import platform_help as ph
from onec_vecgraph.lite import server as lite_server

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git не найден в PATH")

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_BASE_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-0000000000dd">
  <name>ТестБаза</name>
</mdclass:Configuration>
"""

_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-0000-0000-0000-0000000000dd">
  <name>Тест</name>
</mdclass:Catalog>
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return proc.stdout


def _seed_repo(work: Path) -> None:
    src = work / "conf" / "src"
    (src / "Configuration").mkdir(parents=True)
    (src / "Configuration" / "Configuration.mdo").write_text(_BASE_CONFIG, encoding="utf-8")
    (src / "Catalogs" / "Тест").mkdir(parents=True)
    (src / "Catalogs" / "Тест" / "Тест.mdo").write_text(_CATALOG, encoding="utf-8")


@pytest.fixture()
def remote_and_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """(bare remote, рабочая копия-«зеркало пользователя», авторский клон для новых коммитов)."""
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state" / "config.json"))
    for var in ("ONEC_LITE_WORKSPACE", "ONEC_LITE_ROOT", "ONEC_LITE_EXT_ROOTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(lite_server, "_WORKSPACES", {})
    monkeypatch.setattr(lite_server, "_UPDATE_RESULTS", {})
    monkeypatch.setattr(lite_server, "_HELP", ph.HelpCatalog())
    monkeypatch.setattr(lite_server, "_HELP_INIT", True)

    seed = tmp_path / "seed"
    _seed_repo(seed)
    _git(seed, "init", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    author = tmp_path / "author"
    _git(tmp_path, "clone", str(bare), str(author))
    return bare, clone, author


def _push_new_module(author: Path, text: str = "Процедура Новая()\nКонецПроцедуры\n") -> None:
    mod = author / "conf" / "src" / "Catalogs" / "Тест" / "ObjectModule.bsl"
    mod.write_text(text, encoding="utf-8")
    _git(author, "add", "-A")
    _git(author, "commit", "-m", "add module")
    _git(author, "push", "origin", "main")


def test_guards_nonrepo_dirty_detached(tmp_path: Path, remote_and_clone) -> None:
    _bare, clone, _author = remote_and_clone
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitops.status_brief(plain) == {"git": False}
    assert gitops.fetch(plain)["ok"] is False
    assert "не git" in gitops.pull_ff(plain)["error"]
    # грязное дерево -> pull отменяется
    (clone / "мусор.txt").write_text("x", encoding="utf-8")
    res = gitops.pull_ff(clone)
    assert res["ok"] is False and "не чистое" in res["error"]
    (clone / "мусор.txt").unlink()
    # detached HEAD -> pull отменяется
    _git(clone, "checkout", "--detach", "HEAD")
    res2 = gitops.pull_ff(clone)
    assert res2["ok"] is False and "detached" in res2["error"]
    _git(clone, "checkout", "main")


def test_fetch_safe_then_pull_ff(remote_and_clone) -> None:
    _bare, clone, author = remote_and_clone
    _push_new_module(author)
    mod = clone / "conf" / "src" / "Catalogs" / "Тест" / "ObjectModule.bsl"

    res = gitops.fetch(clone)
    assert res["ok"] and res["op"] == "fetch"
    assert res.get("behind") == 1
    assert not mod.exists()  # fetch не трогает дерево

    res2 = gitops.pull_ff(clone)
    assert res2["ok"] and res2["op"] == "pull"
    assert mod.exists()
    assert gitops.status_brief(clone).get("behind") == 0


def test_mirror_clone_then_pull(remote_and_clone) -> None:
    bare, _clone, author = remote_and_clone
    res = gitops.update_mirror("m1", str(bare))
    assert res["ok"] and res["op"] == "mirror-clone"
    dest = gitops.mirror_path("m1")
    assert (dest / ".git").is_dir()
    assert (dest / "conf" / "src" / "Configuration" / "Configuration.mdo").is_file()

    _push_new_module(author)
    res2 = gitops.update_mirror("m1", str(bare))
    assert res2["ok"] and res2["op"] == "mirror-pull"
    assert (dest / "conf" / "src" / "Catalogs" / "Тест" / "ObjectModule.bsl").is_file()


def test_update_workspace_dispatcher(remote_and_clone) -> None:
    bare, clone, _author = remote_and_clone
    path_entry = {"root": str(clone)}
    assert gitops.update_workspace("w", path_entry)["op"] == "fetch"
    assert gitops.update_workspace("w", path_entry, mode="pull")["op"] == "pull"
    mirror_entry = {"repo": str(bare)}
    assert gitops.update_workspace("m2", mirror_entry)["op"] == "mirror-clone"
    assert gitops.update_workspace("m2", mirror_entry)["op"] == "mirror-pull"


def test_admin_apply_repo_registers_mirror(remote_and_clone) -> None:
    bare, _clone, author = remote_and_clone
    snap, err = lite_server.apply_admin_paths(
        "", "", name="m3", repo=str(bare), update_on_start="fetch")
    assert err is None and snap is not None
    row = next(w for w in snap["workspaces"] if w["name"] == "m3")
    assert row["kind"] == "mirror" and row["cloned"] is True
    assert row["update_on_start"] == "fetch"
    wss, _a = lite_admin.load_workspaces(lite_admin.state_file())
    assert wss["m3"]["repo"] == str(bare) and wss["m3"]["root"] == ""

    # воркспейс работает из зеркала
    objs = lite_server.list_objects("Catalog", workspace="m3")
    assert [o["name"] for o in objs["objects"]] == ["Тест"]

    # новый коммит в remote -> кнопка «обновить» (той же логикой, что route)
    _push_new_module(author)
    res = gitops.update_workspace("m3", wss["m3"])
    assert res["ok"] and res["op"] == "mirror-pull"
    rts = lite_server.list_routines("Catalog", "Тест", module="Object", workspace="m3")
    assert [r["name"] for r in rts["routines"]] == ["Новая"]


def test_update_on_start_fetch_runs_once(remote_and_clone) -> None:
    _bare, clone, author = remote_and_clone
    lite_admin.upsert_workspace(lite_admin.state_file(), "w1", str(clone), [],
                                update_on_start="fetch")
    _push_new_module(author)
    lite_server._ws("w1")  # noqa: SLF001 - ленивое построение запускает fetch
    res = lite_server._UPDATE_RESULTS.get("w1")  # noqa: SLF001
    assert res and res["ok"] and res["op"] == "fetch" and res["trigger"] == "on_start"
    assert res.get("behind") == 1
    mod = clone / "conf" / "src" / "Catalogs" / "Тест" / "ObjectModule.bsl"
    assert not mod.exists()  # fetch не трогает дерево


def test_launcher_update_fetch_and_pull(remote_and_clone,
                                        capsys: pytest.CaptureFixture) -> None:
    _bare, clone, author = remote_and_clone
    lite_admin.upsert_workspace(lite_admin.state_file(), "w2", str(clone), [])
    _push_new_module(author)
    assert launcher.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "fetch — ok" in out and "отстаёт на 1" in out
    assert launcher.main(["update", "--pull"]) == 0
    out2 = capsys.readouterr().out
    assert "pull — ok" in out2
    assert (clone / "conf" / "src" / "Catalogs" / "Тест" / "ObjectModule.bsl").exists()
