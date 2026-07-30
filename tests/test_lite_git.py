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


def test_review_set_includes_untracked_against_ref(git_ws: Workspace) -> None:
    """При заданном ref новый (untracked) модуль обязан попасть в ревью-набор: иначе только что
    созданный код молча не проходит ревью. Раньше review_set шёл с untracked=False, хотя
    changed_objects эту полноту защищал."""
    res = gitview.review_set(git_ws, ref="HEAD")
    objects = {r["object"] for r in res["routines"]}
    assert "Catalog.Товары" in objects  # ManagerModule.bsl создан и не добавлен в git
    assert any(r["status"] == "??" for r in res["routines"])
    # счёт затронутых рутин — это счёт, а не лимит выдачи
    assert res["routine_count"] == len(
        [r for r in gitview.review_set(git_ws, ref="HEAD", max_routines=999)["routines"]])


def test_review_set_maps_hunks_to_routines_and_callers(git_ws: Workspace) -> None:
    res = gitview.review_set(git_ws)
    by_routine = {(r["object"], r["routine"]): r for r in res["routines"]}
    # правленая строка попала в тело ПроверитьКод -> рутина в наборе, НеТронутая - нет
    touched = by_routine[("CommonModule.Проверки", "ПроверитьКод")]
    assert touched["export"] is True and touched["status"] == "M"
    # по умолчанию вызывающие — компактные строки «Объект▸Модуль▸Рутина[:строка]»
    assert any("ПередЗаписью" in c for c in touched["callers"])
    assert touched["callers_count"] >= 1
    # detail=True возвращает полные записи (с координатой вызова)
    detailed = gitview.review_set(git_ws, detail=True)
    d_touched = {(r["object"], r["routine"]): r for r in detailed["routines"]}[
        ("CommonModule.Проверки", "ПроверитьКод")]
    callers = {c["routine"] for c in d_touched["callers"]}
    assert "ПередЗаписью" in callers
    assert all("call_line" in c for c in d_touched["callers"])
    assert ("CommonModule.Проверки", "НеТронутая") not in by_routine
    # untracked-модуль включается целиком
    new = by_routine[("Catalog.Товары", "НоваяФункция")]
    assert new["status"] == "??" and new["callers"] == []
    # флаги говорят каждый о своём: окно полно и предохранитель не срабатывал
    assert res["routine_count"] == 2
    assert res["window_incomplete"] is False and res["safety_valve_fired"] is False


_NEW_MODULE_MDO = _COMMON.replace("Проверки", "МодульНовый").replace("22222222", "33333333")
_NEW_MODULE_BSL = """Процедура СовсемНоваяРутина() Экспорт
    Возврат;
КонецПроцедуры
"""


def test_review_set_sees_routines_in_a_brand_new_directory(git_ws: Workspace) -> None:
    """Новый КАТАЛОГ объекта: git status сворачивает его в одну запись без -uall.

    Из-за этого рутины только что созданного модуля не попадали в ревью незакоммиченной
    работы (ref=""), причём молча: window_incomplete и safety_valve_fired оставались false,
    и агент считал набор полным."""
    src = Path(git_ws.sources[0].files_root)
    _w(src / "CommonModules" / "МодульНовый" / "МодульНовый.mdo", _NEW_MODULE_MDO)
    _w(src / "CommonModules" / "МодульНовый" / "Module.bsl", _NEW_MODULE_BSL)
    res = gitview.review_set(git_ws, detail=True, max_routines=50)
    assert "CommonModule.МодульНовый::СовсемНоваяРутина" in {
        f"{r['object']}::{r['routine']}" for r in res["routines"]
    }


def test_review_set_drops_foreign_same_named_callers(git_ws: Workspace) -> None:
    """Одноимённый метод чужого общего модуля — не вызывающий нашей форменной рутины.

    `МодульРаботаСФайламиКлиент.ПриОткрытии()` вызывает метод ТОГО модуля; раньше такие строки
    попадали в ревью-набор как вызывающие изменённого обработчика формы (на боевом ревью —
    154 строки) и вдобавок поднимали рутину в ранжировании по риску."""
    src = Path(git_ws.sources[0].files_root)
    # общий модуль с методом ПриОткрытии + его вызов из третьего места
    _w(src / "CommonModules" / "Файлы" / "Файлы.mdo",
       _COMMON.replace("Проверки", "Файлы").replace("22222222", "44444444"))
    _w(src / "CommonModules" / "Файлы" / "Module.bsl",
       "Процедура ПриОткрытии(Отказ) Экспорт\n    Возврат;\nКонецПроцедуры\n")
    _w(src / "Catalogs" / "Товары" / "Forms" / "ФормаЭлемента" / "Module.bsl",
       "Процедура ПриОткрытии(Отказ)\n    Возврат;\nКонецПроцедуры\n")
    _git(["add", "-A"], Path(git_ws.root))
    _git(["commit", "-q", "-m", "forms"], Path(git_ws.root))
    _w(src / "Catalogs" / "Товары" / "ObjectModule.bsl",
       _OBJ_BSL + "\nПроцедура Тест()\n    Файлы.ПриОткрытии(Ложь);\nКонецПроцедуры\n")
    # правим саму форменную рутину, чтобы она попала в набор
    _w(src / "Catalogs" / "Товары" / "Forms" / "ФормаЭлемента" / "Module.bsl",
       "Процедура ПриОткрытии(Отказ)\n    Отказ = Ложь;\nКонецПроцедуры\n")
    res = gitview.review_set(git_ws, detail=True, max_routines=50)
    form_rows = [r for r in res["routines"]
                 if r["routine"] == "ПриОткрытии" and str(r.get("module")).startswith("Form:")]
    assert form_rows, "изменённый обработчик формы должен попасть в набор"
    for row in form_rows:
        quals = [(c.get("qualifier") or "").lower() for c in row.get("callers") or []]
        assert "файлы" not in quals


def test_review_set_keeps_form_method_called_through_a_form_reference(git_ws: Workspace) -> None:
    """Экспортный метод модуля формы ШТАТНО вызывается снаружи по ссылке на форму.

    Идиома 1С: `&НаКлиентеНаСервереБезКонтекста Процедура X(Форма)` → `Форма.МетодЭкспорт()`.
    Правило «у форменной цели любой чужой квалификатор доказывает другую цель» было неверным и
    молча обнуляло вызывающих (на УТ — 242 реальных места вызова), причём find_callers на тот же
    вопрос отвечал верно. Отсекаем только доказуемо чужое — квалификатор-имя общего модуля."""
    src = Path(git_ws.sources[0].files_root)
    form = src / "Catalogs" / "Товары" / "Forms" / "ФормаЭлемента" / "Module.bsl"
    _w(form, "Функция ПолучитьПоля() Экспорт\n    Возврат 1;\nКонецФункции\n")
    _w(src / "CommonModules" / "Помощник" / "Помощник.mdo",
       _COMMON.replace("Проверки", "Помощник").replace("22222222", "77777777"))
    _w(src / "CommonModules" / "Помощник" / "Module.bsl",
       "Процедура Обработать(Форма) Экспорт\n    Поля = Форма.ПолучитьПоля();\nКонецПроцедуры\n")
    _git(["add", "-A"], Path(git_ws.root))
    _git(["commit", "-q", "-m", "form+helper"], Path(git_ws.root))
    _w(form, "Функция ПолучитьПоля() Экспорт\n    Возврат 2;\nКонецФункции\n")
    res = gitview.review_set(git_ws, detail=True, max_routines=50)
    rows = [r for r in res["routines"] if r["routine"] == "ПолучитьПоля"]
    assert rows, "изменённый метод формы должен попасть в набор"
    assert rows[0]["callers_count"] == 1, rows[0]
    assert rows[0].get("callers_foreign_dropped", 0) == 0, rows[0]


_PULLED_OVERRIDE = """&Вместо("ПроверитьКод")
Функция Расш_ПроверитьКод(Знач Код) Экспорт
    Возврат Истина;
КонецФункции
"""


def _build_index(ws: Workspace) -> None:
    from onec_vecgraph.lite import code_intel, fts

    fts.index_for(ws).build(wait=60)
    code_intel.clear_caches()


def test_committed_pull_is_visible_in_declarations_and_overrides(git_ws: Workspace) -> None:
    """Файлы, пришедшие КОММИТОМ после сборки индекса (git pull / onec-lite sync), обязаны
    попадать в find_routine и find_overrides.

    Живой подмес брал только «грязный» набор (git status), а подтянутое уже закоммичено —
    значит в него не входило. При одноимённых объявлениях в других объектах ответ оставался
    непустым, фолбэк на скан не срабатывал, и новое объявление просто отсутствовало без
    единого флага неполноты."""
    from onec_vecgraph.lite import code_intel, fts

    pytest.importorskip("sqlite3")
    if not fts.fts_available():
        pytest.skip("sqlite3 without FTS5")

    root = Path(git_ws.root)
    src = Path(git_ws.sources[0].files_root)
    # приводим копию к чистому состоянию и строим индекс на ней
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "base2"], root)
    _build_index(git_ws)
    before = code_intel.find_declarations(git_ws, "ПроверитьКод", max_results=50)
    assert before["engine"] == "index" and before["declaration_count"] == 1

    # «pull»: новый объект с одноимённой рутиной + расширение с перехватом — и всё ЗАКОММИЧЕНО
    _w(src / "CommonModules" / "Ещё" / "Ещё.mdo",
       _COMMON.replace("Проверки", "Ещё").replace("22222222", "55555555"))
    _w(src / "CommonModules" / "Ещё" / "Module.bsl",
       "Функция ПроверитьКод(Знач Код) Экспорт\n    Возврат Ложь;\nКонецФункции\n")
    ext = src.parent.parent / "расш" / "src"
    _w(ext / "Configuration" / "Configuration.mdo",
       _CONFIG.replace("ГитБаза", "РасшГит").replace("00000000", "66666666").replace(
           "</mdclass:Configuration>",
           "  <configurationExtensionPurpose>AddOn</configurationExtensionPurpose>\n"
           "</mdclass:Configuration>"))
    _w(ext / "CommonModules" / "Проверки" / "Module.bsl", _PULLED_OVERRIDE)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "pulled"], root)

    wider = Workspace(root, ext_roots=(str(ext.parent),))
    code_intel.clear_caches()
    after = code_intel.find_declarations(wider, "ПроверитьКод", max_results=50)
    objects = {r.get("object") for r in after["declarations"]}
    assert "CommonModule.Ещё" in objects, after     # подтянутое объявление видно
    assert "CommonModule.Проверки" in objects       # прежнее не потеряно

    overrides = code_intel.find_overrides(wider, max_results=50)
    rows = overrides.get("overrides") or overrides.get("rows") or []
    assert any(r.get("routine") == "Расш_ПроверитьКод" for r in rows), overrides
