"""Lite git-awareness: changed_objects / review_set over a temp git repo (skip без git).

Строим мини EDT-воркспейс, коммитим, правим модуль (+ добавляем untracked) и проверяем
маппинг статусов на объекты и диффенутых строк — на рутины с вызывающими."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from onec_vecgraph.lite import Workspace, gitview

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-0000000000bb">
  <name>ГитБаза</name>
</mdclass:Configuration>
"""

_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-0000-0000-0000-0000000000bb">
  <name>Товары</name>
</mdclass:Catalog>
"""

_COMMON = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:CommonModule {_MDCLASS} uuid="22222222-0000-0000-0000-0000000000bb">
  <name>Проверки</name>
</mdclass:CommonModule>
"""

_COMMON_BSL_V1 = """Функция ПроверитьКод(Знач Код) Экспорт
    Возврат Код > 0;
КонецФункции

Процедура НеТронутая()
    Возврат;
КонецПроцедуры
"""

# Правка внутри тела ПроверитьКод (одна строка) — НеТронутая не задета.
_COMMON_BSL_V2 = """Функция ПроверитьКод(Знач Код) Экспорт
    Возврат Код >= 0;
КонецФункции

Процедура НеТронутая()
    Возврат;
КонецПроцедуры
"""

_OBJ_BSL = """Процедура ПередЗаписью(Отказ)
    Если Не Проверки.ПроверитьКод(Код) Тогда
        Отказ = Истина;
    КонецЕсли;
КонецПроцедуры
"""

_NEW_MANAGER_BSL = """Функция НоваяФункция() Экспорт
    Возврат Истина;
КонецФункции
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def git_ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    src = root / "conf" / "src"
    _w(src / "Configuration" / "Configuration.mdo", _CONFIG)
    _w(src / "Catalogs" / "Товары" / "Товары.mdo", _CATALOG)
    _w(src / "Catalogs" / "Товары" / "ObjectModule.bsl", _OBJ_BSL)
    _w(src / "CommonModules" / "Проверки" / "Проверки.mdo", _COMMON)
    _w(src / "CommonModules" / "Проверки" / "Module.bsl", _COMMON_BSL_V1)
    _git(["init", "-q"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "base"], root)
    # незакоммиченная работа: правка общего модуля + новый (untracked) менеджерский модуль
    _w(src / "CommonModules" / "Проверки" / "Module.bsl", _COMMON_BSL_V2)
    _w(src / "Catalogs" / "Товары" / "ManagerModule.bsl", _NEW_MANAGER_BSL)
    return Workspace(root)


@pytest.fixture(autouse=True)
def _no_rg(monkeypatch: pytest.MonkeyPatch) -> None:
    from onec_vecgraph.lite import search as lite_search

    monkeypatch.setattr(lite_search, "rg_path", lambda: None)


def test_changed_objects_groups_status_by_object(git_ws: Workspace) -> None:
    res = gitview.changed_objects(git_ws)
    assert res["object_count"] == 2
    by_obj = {o["object"]: o for o in res["objects"]}
    common = by_obj["CommonModule.Проверки"]
    assert [(c["status"], c["artifact"]) for c in common["changes"]] == [("M", "module")]
    catalog = by_obj["Catalog.Товары"]
    assert [(c["status"], c["artifact"]) for c in catalog["changes"]] == [("??", "module")]
    assert res["repos"] and res["repos"][0].get("branch")


def test_changed_objects_against_ref_and_non_git(git_ws: Workspace, tmp_path: Path) -> None:
    res = gitview.changed_objects(git_ws, ref="HEAD")
    assert {o["object"] for o in res["objects"]} == {"CommonModule.Проверки", "Catalog.Товары"}
    # не-git рабочая копия -> понятная ошибка по репозиторию, не исключение
    plain = tmp_path / "plain"
    _w(plain / "conf" / "src" / "Configuration" / "Configuration.mdo", _CONFIG)
    _w(plain / "conf" / "src" / "Catalogs" / "Т" / "Т.mdo", _CATALOG)
    res2 = gitview.changed_objects(Workspace(plain))
    assert res2["objects"] == []
    assert any("недоступен" in r.get("error", "") for r in res2["repos"])


def test_git_invocation_opts_out_of_dubious_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    """_git всегда передаёт safe.directory=* — иначе git отказывает ("dubious ownership")
    на смонтированных в контейнер репозиториях с чужим владельцем (корневая причина
    пустого дифа на песочнице ГТ: serve-lite в Docker под uid != владельца /dumps)."""
    captured: dict[str, list[str]] = {}

    class _Proc:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv: list[str], **_kw: object) -> _Proc:
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(gitview.subprocess, "run", fake_run)
    gitview._git(["rev-parse", "--show-toplevel"], Path("."))
    assert captured["argv"][0] == "git"
    assert "safe.directory=*" in captured["argv"]


def test_review_set_maps_hunks_to_routines_and_callers(git_ws: Workspace) -> None:
    res = gitview.review_set(git_ws)
    by_routine = {(r["object"], r["routine"]): r for r in res["routines"]}
    # правленая строка попала в тело ПроверитьКод -> рутина в наборе, НеТронутая - нет
    touched = by_routine[("CommonModule.Проверки", "ПроверитьКод")]
    assert touched["export"] is True and touched["status"] == "M"
    callers = {c["routine"] for c in touched["callers"]}
    assert "ПередЗаписью" in callers
    assert ("CommonModule.Проверки", "НеТронутая") not in by_routine
    # untracked-модуль включается целиком
    new = by_routine[("Catalog.Товары", "НоваяФункция")]
    assert new["status"] == "??" and new["callers"] == []
    assert res["routine_count"] == 2 and res["routines_truncated"] is False
