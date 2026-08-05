"""Lite FTS5 index: BM25 ranking, CamelCase tokens, Cyrillic endings, mtime increments.

БД кладётся во временный state-каталог (ONEC_LITE_STATE), не в рабочую копию."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from onec_vecgraph.lite import Workspace, fts

pytestmark = pytest.mark.skipif(not fts.fts_available(), reason="sqlite3 without FTS5")

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-0000000000cc">
  <name>ФтсБаза</name>
</mdclass:Configuration>
"""

_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-0000-0000-0000-0000000000cc">
  <name>Номенклатура</name>
  <synonym><key>ru</key><value>Товары и услуги</value></synonym>
  <attributes uuid="11111111-0000-0000-0000-0000000000c1">
    <name>СебестоимостьПлановая</name>
    <synonym><key>ru</key><value>Плановая себестоимость</value></synonym>
    <type><types>Number</types></type>
  </attributes>
</mdclass:Catalog>
"""

_COMMON = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:CommonModule {_MDCLASS} uuid="22222222-0000-0000-0000-0000000000cc">
  <name>РасчетЗатрат</name>
</mdclass:CommonModule>
"""

_COMMON_BSL = """Функция РассчитатьСебестоимость(Номенклатура) Экспорт
    // считаем себестоимости партий
    Возврат 0;
КонецФункции

Процедура ПрочаяРабота()
    // тут упоминается себестоимость только в комментарии
КонецПроцедуры
"""


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    # state (и БД индекса) — отдельно от рабочей копии, как в реальной жизни
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state" / "config.json"))
    src = tmp_path / "repo" / "conf" / "src"
    _w(src / "Configuration" / "Configuration.mdo", _CONFIG)
    _w(src / "Catalogs" / "Номенклатура" / "Номенклатура.mdo", _CATALOG)
    _w(src / "CommonModules" / "РасчетЗатрат" / "РасчетЗатрат.mdo", _COMMON)
    _w(src / "CommonModules" / "РасчетЗатрат" / "Module.bsl", _COMMON_BSL)
    return Workspace(tmp_path / "repo")


def test_fts_query_builder() -> None:
    q = fts._fts_query("где считается себестоимость")
    # усечение на 2 символа: префикс «себестоимос» матчит и «…ость», и «…ости»
    assert '("себестоимость" OR "себестоимос"*)' in q
    assert '("считается" OR "считает"*)' in q
    assert '"где"' in q  # короткие токены — как есть
    assert fts._fts_query("") == ""


def test_build_search_rank_and_freshness(ws: Workspace) -> None:
    idx = fts.index_for(ws)
    assert idx.status()["built"] is False

    stats = idx.build()
    assert stats["units_written"] >= 4  # 2 рутины + 2 карточки объектов (+конфигурация?)
    st = idx.status()
    assert st["built"] and st["units"] == stats["units_written"]
    assert not str(fts.db_path_for(ws)).startswith(str(ws.root))  # БД вне рабочей копии

    res = idx.search("где считается себестоимость")
    assert res["match_count"] >= 2 and res["built_at"]
    titles = [r["title"] for r in res["results"]]
    # имя рутины весит больше упоминания в комментарии
    assert titles[0] == "РассчитатьСебестоимость"
    assert "ПрочаяРабота" in titles
    top = res["results"][0]
    assert top["unit"] == "routine" and top["object"] == "CommonModule.РасчетЗатрат"
    assert top["line"] == 1 and top["path"].endswith("Module.bsl")

    # карточка объекта находится по синониму реквизита
    obj = idx.search("плановая себестоимость", unit="object")
    assert obj["results"] and obj["results"][0]["title"] == "Catalog.Номенклатура"


def test_incremental_update_and_delete(ws: Workspace) -> None:
    idx = fts.index_for(ws)
    idx.build()
    module = ws.root / "conf" / "src" / "CommonModules" / "РасчетЗатрат" / "Module.bsl"
    text = module.read_text(encoding="utf-8") + (
        "\nФункция НоваяМаржинальность() Экспорт\n    Возврат 1;\nКонецФункции\n"
    )
    module.write_text(text, encoding="utf-8")
    future = time.time() + 5
    import os

    os.utime(module, (future, future))  # гарантированно другой mtime на грубых ФС
    stats = idx.build()
    assert (stats["files_updated"], stats["files_added"]) == (1, 0), stats
    assert idx.search("маржинальность")["results"][0]["title"] == "НоваяМаржинальность"

    module.unlink()
    stats2 = idx.build()
    assert stats2["files_removed"] == 1
    assert idx.search("маржинальность")["match_count"] == 0


def test_search_auto_refresh_by_ttl(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    idx = fts.index_for(ws)
    idx.build()
    module = ws.root / "conf" / "src" / "CommonModules" / "РасчетЗатрат" / "Module.bsl"
    text = module.read_text(encoding="utf-8") + (
        "\nПроцедура СовсемНовая()\nКонецПроцедуры\n"
    )
    module.write_text(text, encoding="utf-8")
    import os

    future = time.time() + 5
    os.utime(module, (future, future))
    monkeypatch.setattr(fts, "_REFRESH_TTL", 0.0)  # каждый поиск запускает фоновый рефреш
    # рефреш теперь неблокирующий (в фоне) — результат появляется в течение пары циклов
    deadline = time.time() + 5.0
    matched = 0
    while time.time() < deadline:
        matched = idx.search("СовсемНовая").get("match_count", 0)
        if matched:
            break
        time.sleep(0.05)
    assert matched == 1


def test_search_before_build_is_soft_not_ready(ws: Workspace) -> None:
    """До построения индекса поиск НЕ отдаёт ошибку/пусто, а сообщает ready=False (чтобы
    вызывающий деградировал на search_code) и запускает фоновую сборку."""
    idx = fts.index_for(ws)
    res = idx.search("себестоимость")
    assert res.get("ready") is False
    assert "error" not in res and res.get("note")


def test_explicit_build_yields_to_running_background(ws: Workspace) -> None:
    """Пока идёт сборка (флаг _building), синхронный build() не встаёт вторым писателем,
    а ensure_background не запускает вторую — единственный писатель гарантирован."""
    idx = fts.index_for(ws)
    idx._building = True
    try:
        # wait=0: не ждём чужую сборку — проверяем именно отказ стать вторым писателем
        assert idx.build(wait=0).get("status") == "building"
        assert idx.ensure_background(force=True) is False
    finally:
        idx._building = False
    assert idx.build().get("units_written", 0) >= 1  # после снятия флага сборка снова идёт


def test_fts_build_lock_is_cross_process(ws: Workspace) -> None:
    """Межпроцессный лок: пока держится `<db>.building`, _build_locked пропускает сборку
    (status=building) — защита от гонки двух серверов за общий каталог ~/.onec-lite/fts."""
    idx = fts.index_for(ws)
    handle = fts._acquire_build_lock(idx.path)          # эмулируем «строит другой процесс»
    assert handle is not None
    assert fts._acquire_build_lock(idx.path) is None    # второй захват — отказ, пока держим
    try:
        res = idx._build_locked()
        assert res.get("status") == "building"          # не строил, уступил
    finally:
        fts._release_build_lock(handle)
    assert idx.build().get("units_written", 0) >= 1     # лок отпущен — сборка снова проходит


def test_index_sees_fresh_work_without_waiting_for_refresh(ws: Workspace) -> None:
    """Индекс не должен «уверенно врать» про свежую работу.

    Критика круга 2: после добавления вызова в уже проиндексированный файл (и после создания
    нового модуля) индексный путь отдавал прежний ответ — свежих вызовов он не видит, а строк
    по такому файлу в выборке нет, поэтому пометки `stale` не будет. Теперь «грязный» по git
    набор всегда разбирается живым парсером и подмешивается к индексу."""
    import subprocess

    from onec_vecgraph.lite import code_intel

    root = ws.root
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
                   cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "-m", "base"], cwd=str(root), check=True, capture_output=True)

    idx = fts.index_for(ws)
    assert "error" not in idx.build()
    assert idx.has_symbols()
    before = code_intel.find_callers(ws, "РассчитатьСебестоимость")["match_count"]

    # новый модуль с вызовом — файла нет ни в индексе, ни в git
    new_mod = root / "conf" / "src" / "CommonModules" / "РасчетЗатрат" / "ManagerModule.bsl"
    _w(new_mod, "Процедура СвежийВызов() Экспорт\n"
                "    РасчетЗатрат.РассчитатьСебестоимость(Неопределено);\n"
                "КонецПроцедуры\n")
    code_intel.clear_caches()

    after = code_intel.find_callers(ws, "РассчитатьСебестоимость")
    callers = {c["routine"] for c in after["callers"]}
    assert after["match_count"] == before + 1, after
    assert "СвежийВызов" in callers


def test_index_is_not_truth_for_declarations_and_overrides(ws: Workspace) -> None:
    """Индекс не должен считаться истиной: удалённая рутина не выдаётся, добавленная видна.

    Критика круга 3: `declarations()`/`overrides()`, в отличие от `callers_of`, не проверяли ни
    mtime, ни «грязный» набор. Доказанные последствия: после удаления рутины инструмент отдавал
    её прежние координаты (агент читал по ним ЧУЖОЙ код), после добавления экспортной рутины —
    «такой рутины нет», а новый хук `&Вместо(...)` в расширении оставался невидим (и не попадал
    в review_set.overridden_by)."""
    from onec_vecgraph.lite import code_intel

    module = ws.root / "conf" / "src" / "CommonModules" / "РасчетЗатрат" / "Module.bsl"
    idx = fts.index_for(ws)
    assert "error" not in idx.build()
    assert idx.has_symbols()
    assert code_intel.find_declarations(ws, "РассчитатьСебестоимость")["declaration_count"] == 1

    # рутину переименовали: старого имени больше нет, новое — есть, индекс ещё не пересобран
    module.write_text(
        "Функция ПереименованнаяСебестоимость(Номенклатура) Экспорт\n    Возврат 0;\nКонецФункции\n",
        encoding="utf-8")
    code_intel.clear_caches()

    gone = code_intel.find_declarations(ws, "РассчитатьСебестоимость")
    assert gone["declaration_count"] == 0, gone      # прежние координаты не выдаются
    fresh = code_intel.find_declarations(ws, "ПереименованнаяСебестоимость")
    assert fresh["declaration_count"] == 1, fresh    # добавленная рутина видна сразу
    assert fresh["declarations"][0]["lines"] == [1, 3]


def _git_init(root: Path) -> None:
    import subprocess

    def g(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=str(root), check=True, capture_output=True)

    g("init", "-q")
    g("add", "-A")
    g("commit", "-q", "-m", "base")


def _commit_all(root: Path, msg: str) -> None:
    import subprocess

    for args in (("add", "-A"), ("commit", "-q", "-m", msg)):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=str(root), check=True, capture_output=True)


def test_index_sees_change_committed_after_the_build(ws: Workspace) -> None:
    """ЗАКОММИЧЕННАЯ после сборки правка обязана быть видна.

    Живым разбором подмешивалось только незакоммиченное (git status), а строк по закоммиченному
    файлу в выборке нет — значит и `stale` для него не будет. Ответ был уверенным и неверным:
    одно место вызова вместо двух, `truncated: false`, без пометок. Теперь коммит сборки лежит
    в meta, и файлы, закоммиченные после него, разбираются живым парсером."""
    from onec_vecgraph.lite import code_intel

    src = Path(ws.sources[0].files_root)
    _w(src / "CommonModules" / "Потребитель" / "Потребитель.mdo",
       _COMMON.replace("РасчетЗатрат", "Потребитель").replace("22222222", "55555555"))
    _w(src / "CommonModules" / "Потребитель" / "Module.bsl",
       "Процедура Первый()\n    РасчетЗатрат.РассчитатьСебестоимость(1);\nКонецПроцедуры\n")
    _git_init(Path(ws.root))
    code_intel.clear_caches()
    fts.index_for(ws).build(wait=0)
    base = code_intel.find_callers(ws, "РассчитатьСебестоимость", max_results=50)
    assert base["engine"] == "index" and base["call_rows_total"] == 1

    # новый вызов в ДРУГОМ модуле, и он ЗАКОММИЧЕН — рабочая копия чистая
    _w(src / "CommonModules" / "Второй" / "Второй.mdo",
       _COMMON.replace("РасчетЗатрат", "Второй").replace("22222222", "66666666"))
    _w(src / "CommonModules" / "Второй" / "Module.bsl",
       "Процедура Второй()\n    РасчетЗатрат.РассчитатьСебестоимость(2);\nКонецПроцедуры\n")
    _commit_all(Path(ws.root), "новый вызов")
    code_intel.clear_caches()
    res = code_intel.find_callers(ws, "РассчитатьСебестоимость", max_results=50)
    assert res["match_count"] == 2, res
    assert res["call_rows_total"] == 2, res


def test_counters_agree_with_rows_on_uncommitted_edit(ws: Workspace) -> None:
    """Счётчики не могут быть МЕНЬШЕ выданных строк.

    Агрегаты брались из SQL, а строки — уже с подмешанной живой работой: на незакоммиченной
    правке выходило `match_count: 3` при `call_rows_total: 1` и сумме by_object == 1 без флага
    обрезки. Это ловит собственный инвариант проекта (`shown > total → FAIL`)."""
    from onec_vecgraph.lite import code_intel

    src = Path(ws.sources[0].files_root)
    _w(src / "CommonModules" / "Потребитель" / "Потребитель.mdo",
       _COMMON.replace("РасчетЗатрат", "Потребитель").replace("22222222", "55555555"))
    _w(src / "CommonModules" / "Потребитель" / "Module.bsl",
       "Процедура Первый()\n    РасчетЗатрат.РассчитатьСебестоимость(1);\nКонецПроцедуры\n")
    _git_init(Path(ws.root))
    code_intel.clear_caches()
    fts.index_for(ws).build(wait=0)

    _w(src / "CommonModules" / "Потребитель" / "Module.bsl",
       "Процедура Первый()\n    РасчетЗатрат.РассчитатьСебестоимость(1);\n"
       "    РасчетЗатрат.РассчитатьСебестоимость(2);\nКонецПроцедуры\n"
       "Процедура Третий()\n    РасчетЗатрат.РассчитатьСебестоимость(3);\nКонецПроцедуры\n")
    code_intel.clear_caches()
    res = code_intel.find_callers(ws, "РассчитатьСебестоимость", max_results=50)
    assert res["match_count"] == 3, res
    assert res["call_rows_total"] >= res["match_count"], res
    assert res["uncommitted_merged"] is True
    by_sum = sum(x["count"] for x in res["by_object"])
    assert by_sum == res["call_rows_total"], res


def _many_declarations(ws: Workspace, count: int) -> None:
    """Разложить одноимённую рутину по многим объектам — чтобы скан гарантированно обрезался."""
    src = Path(ws.sources[0].files_root)
    for i in range(count):
        name = f"Объект{i:03d}"
        _w(src / "Catalogs" / name / f"{name}.mdo",
           _CATALOG.replace("Номенклатура", name).replace("11111111", f"{i:08d}"))
        _w(src / "Catalogs" / name / "ObjectModule.bsl",
           "Процедура ПередЗаписью(Отказ)\n    Возврат;\nКонецПроцедуры\n")


def test_scan_path_reports_unknown_declaration_count_instead_of_window(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обрезанный скан обязан отдать declaration_count=null, а не размер окна.

    Пока стоял len(rows), окно выдавалось за полный счёт: на УТ с пустым индексом `ПередЗаписью`
    давал declaration_count=2 против 1401 на индексном пути, и агент делал вывод, что обработчик
    в этой конфигурации почти не используется. Полный счёт — это счёт; неизвестный — null."""
    from onec_vecgraph.lite import code_intel

    _many_declarations(ws, 12)
    monkeypatch.setattr(fts.FtsIndex, "has_symbols", lambda self: False)
    code_intel.clear_caches()

    cut = code_intel.find_declarations(ws, "ПередЗаписью", max_results=3)
    assert cut["engine"] == "scan"
    assert cut["truncated"] is True
    assert cut["declaration_count"] is None, cut
    assert cut["returned"] == 3

    # необрезанный скан ВИДЕЛ всё — там счёт обязан быть настоящим числом
    full = code_intel.find_declarations(ws, "ПередЗаписью", max_results=500)
    assert full["engine"] == "scan" and full["truncated"] is False
    assert full["declaration_count"] == 12, full


def test_metrics_exposes_index_state(ws: Workspace) -> None:
    """metrics обязан показывать состояние индекса.

    Без этого оператор не видит, что воркспейс сидит на пустом индексе: ответы верны, но идут
    медленным сканом с урезанными счётчиками. Именно так два боевых воркспейса простояли с БД на
    гигабайты и нулём рутин — снаружи всё выглядело работающим."""
    from onec_vecgraph.lite import code_intel

    before = code_intel.metrics(ws)
    assert "index" in before, before
    assert before["index"]["built"] is False

    fts.index_for(ws).build(wait=60)
    code_intel.clear_caches()
    after = code_intel.metrics(ws)
    assert after["index"]["built"] is True
    assert after["index"]["symbols"] > 0 and after["index"]["units"] > 0


def test_type_usages_declares_its_scope(ws: Workspace) -> None:
    """Ответ обязан сам сообщать, что осмотрены только метаданные.

    Имя find_type_usages шире того, что тул делает: обращения из кода (`Справочники.<Имя>`) он не
    считает. Пока это было лишь в описании, агент принимал usage_count за все использования."""
    from onec_vecgraph.lite import code_intel

    res = code_intel.type_usages(ws, "Catalog", "Номенклатура")
    assert res["scope"] == "metadata"
    assert "КОДА" in res["scope_note"] or "код" in res["scope_note"].lower()


_EXT_MDO = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
  uuid="99999999-0000-0000-0000-0000000000cc">
  <name>РасширениеТест</name>
  <configurationExtensionPurpose>AddOn</configurationExtensionPurpose>
</mdclass:Configuration>
"""


def test_index_notices_a_whole_source_appearing(ws: Workspace, tmp_path: Path) -> None:
    """Подключение расширения ПОСЛЕ сборки обязано быть заметно в состоянии индекса.

    Имя БД — хэш только корня, а _is_stale проверяет пофайлово: появление целого источника так
    не поймать. На боевом gt_ut подключение расширения на 689 файлов давало built=true при
    полном отсутствии источника в индексе — до случайного рефреша по TTL."""
    idx = fts.index_for(ws)
    idx.build(wait=60)
    assert idx.status()["built"] is True
    assert idx.status().get("sources_changed") is False

    ext = tmp_path / "ext"
    _w(ext / "src" / "Configuration" / "Configuration.mdo", _EXT_MDO)
    _w(ext / "src" / "CommonModules" / "ДопРасчет" / "ДопРасчет.mdo",
       _COMMON.replace("РасчетЗатрат", "ДопРасчет"))
    _w(ext / "src" / "CommonModules" / "ДопРасчет" / "Module.bsl",
       "Функция ДопиРассчитать() Экспорт\n    Возврат 1;\nКонецФункции\n")

    wider = Workspace(ws.root, ext_roots=(str(ext),))
    assert len(wider.sources) == len(ws.sources) + 1
    widened = fts.FtsIndex(wider)
    st = widened.status()
    assert st["built"] is True            # файл тот же
    assert st["sources_changed"] is True  # но состав разошёлся — и это ВИДНО
    assert "источник" in (st.get("note") or "").lower()

    # рефреш обязан обойти TTL и добрать новый источник
    assert widened.ensure_background() is True
    for _ in range(120):
        if not widened.status().get("building"):
            break
        time.sleep(0.5)
    after = widened.status()
    assert after["sources_changed"] is False, after
    assert widened.has_name("ДопиРассчитать") is True


def test_unreadable_index_is_not_reported_as_empty(ws: Workspace,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибка чтения БД обязана отличаться от «индекс пуст».

    Пока sqlite3.Error глушился, занятая чужой записью БД отдавала built=false — и это читалось
    как «индекса нет». Я сам на этом ошибся: решил, что пять воркспейсов потеряли индексы, хотя в
    файлах лежало 1.2 млн рутин, просто их держал живой сервер."""
    import sqlite3

    idx = fts.index_for(ws)
    idx.build(wait=60)
    assert idx.status()["built"] is True

    def boom(*_a: object, **_k: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fts, "_connect", boom)
    st = idx.status()
    assert st["built"] is None, st            # неизвестно, а не «пусто»
    assert "database is locked" in st["unreadable"]
    assert "НЕ признак отсутствия" in st["note"]

    from onec_vecgraph.lite import server as lite_server

    brief = lite_server._index_brief(ws)
    assert brief["built"] is None, brief      # overview не превращает «неизвестно» в «нет»
    assert brief.get("unreadable")


def test_index_without_source_fingerprint_is_not_trusted(ws: Workspace) -> None:
    """Индекс без отпечатка состава (собран старой версией) обязан считаться разошедшимся.

    Раньше `_sources_changed` возвращал None, и status/ensure_background молча считали состав
    совпадающим. На боевом УТ такой индекс, собранный по пяти источникам, обслуживал воркспейс из
    ШЕСТИ: 689 файлов расширения были невидимы для индексного пути без единого признака
    неполноты. «Подтвердить нечем» — это не «всё в порядке»."""
    import sqlite3

    idx = fts.index_for(ws)
    idx.build(wait=60)
    assert idx.status()["sources_changed"] is False
    con = sqlite3.connect(str(fts.db_path_for(ws)))
    try:
        con.execute("DELETE FROM meta WHERE key='sources'")  # как у сборки старой версии
        con.commit()
    finally:
        con.close()
    st = fts.FtsIndex(ws).status()
    assert st["built"] is True
    assert st["sources_changed"] is True, st
    assert "не подтверждён" in (st.get("note") or "").lower(), st


# --------------------------------------------------------------------------- #
# Быстрая перевалидация через git (обход ФС не нужен)
# --------------------------------------------------------------------------- #

_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git_cmd(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def git_ws(ws: Workspace) -> Workspace:
    """Тот же мини-воркспейс, но под git — иначе быстрый путь недоступен по построению."""
    root = Path(ws.root)
    _git_cmd(["init", "-q"], root)
    _git_cmd(["add", "-A"], root)
    _git_cmd(["commit", "-q", "-m", "base"], root)
    return ws


@_GIT
def test_idle_revalidation_uses_git_delta_not_a_full_walk(git_ws: Workspace) -> None:
    """Холостая перевалидация обязана идти через git, а не обходить файловую систему.

    Полный обход на УТ стоил 4.8 с при НУЛЕ изменений (2.2 с по .bsl + 2.4 с по метаданным);
    git отвечает за 0.7 с. Полный обход остаётся страховкой — не реже раза в час."""
    idx = fts.index_for(git_ws)
    first = idx.build(wait=120)
    assert first["scan"] == "full"          # первая сборка: нечего сравнивать
    again = idx.build(wait=120)
    assert again["scan"] == "git-delta", again
    assert (again["files_added"], again["files_updated"], again["files_removed"]) == (0, 0, 0)


@_GIT
def test_git_delta_picks_up_a_committed_change(git_ws: Workspace) -> None:
    """Закоммиченная после сборки правка подхватывается быстрым путём."""
    idx = fts.index_for(git_ws)
    idx.build(wait=120)
    src = Path(git_ws.sources[0].files_root)
    _w(src / "CommonModules" / "РасчетЗатрат" / "Module.bsl",
       _COMMON_BSL + "\nФункция ПослеКоммита() Экспорт\n    Возврат 1;\nКонецФункции\n")
    _git_cmd(["add", "-A"], Path(git_ws.root))
    _git_cmd(["commit", "-q", "-m", "add routine"], Path(git_ws.root))

    res = idx.build(wait=120)
    assert res["scan"] == "git-delta" and res["files_updated"] == 1, res
    assert idx.has_name("ПослеКоммита") is True


@_GIT
def test_revert_to_committed_state_is_still_rechecked(git_ws: Workspace) -> None:
    """Файл, ВОЗВРАЩЁННЫЙ к закоммиченному состоянию, обязан быть перепроверен.

    Для `git status` он чист, поэтому без списка наблюдения быстрый путь его больше не видит — и
    в индексе остаётся правленая версия. На живом УТ так осталась рутина, которой нет на диске."""
    idx = fts.index_for(git_ws)
    idx.build(wait=120)
    module = Path(git_ws.sources[0].files_root) / "CommonModules" / "РасчетЗатрат" / "Module.bsl"
    original = module.read_text(encoding="utf-8")

    _w(module, original + "\nПроцедура Призрак() Экспорт\n    Возврат;\nКонецПроцедуры\n")
    assert idx.build(wait=120)["files_updated"] == 1
    assert idx.has_name("Призрак") is True   # правка попала в индекс

    _w(module, original)                     # вернули как было — git об этом молчит
    res = idx.build(wait=120)
    assert res["scan"] == "git-delta", res
    assert idx.has_name("Призрак") is False, "призрачная рутина осталась в индексе"


@_GIT
def test_source_set_change_forces_a_full_walk(git_ws: Workspace, tmp_path: Path) -> None:
    """Подключение расширения — полный обход: git-дельта про целый новый источник не знает."""
    idx = fts.index_for(git_ws)
    idx.build(wait=120)
    assert idx.build(wait=120)["scan"] == "git-delta"

    ext = tmp_path / "ext2"
    _w(ext / "src" / "Configuration" / "Configuration.mdo", _EXT_MDO)
    _w(ext / "src" / "CommonModules" / "Новый" / "Новый.mdo",
       _COMMON.replace("РасчетЗатрат", "Новый"))
    _w(ext / "src" / "CommonModules" / "Новый" / "Module.bsl",
       "Функция ИзРасширения() Экспорт\n    Возврат 1;\nКонецФункции\n")
    wider = fts.FtsIndex(Workspace(git_ws.root, ext_roots=(str(ext),)))
    res = wider.build(wait=120)
    assert res["scan"] == "full", res
    assert wider.has_name("ИзРасширения") is True


def test_without_git_falls_back_to_full_walk(ws: Workspace) -> None:
    """Не-git рабочая копия: быстрого пути нет, но перевалидация работает полным обходом."""
    idx = fts.index_for(ws)
    idx.build(wait=120)
    assert idx.build(wait=120)["scan"] == "full"


# --------------------------------------------------------------------------- #
# bsl_sql: агрегаты по индексу
# --------------------------------------------------------------------------- #

def test_bsl_sql_schema_then_aggregate(ws: Workspace) -> None:
    """Пустой sql отдаёт схему, GROUP BY считает агрегат — это дешёвая замена выдаче строками.

    На живом УТ «распределение вызовов метода по объектам» стоило 37 437 токенов через
    find_callers и 462 через один GROUP BY: агрегаты были нашим самым дорогим сценарием."""
    fts.index_for(ws).build(wait=120)

    schema = fts.sql_query(ws)
    assert "symbols" in schema["schema"] and "calls" in schema["schema"]
    assert schema["row_counts"]["symbols"] > 0
    assert schema["examples"] and schema["index"]["live_merge"] is False

    res = fts.sql_query(ws, "SELECT COUNT(*) AS n FROM symbols")
    assert res["columns"] == ["n"] and res["rows"][0][0] == schema["row_counts"]["symbols"]
    # честность: ответ по индексу, живого подмеса тут нет — и это сказано в ответе
    assert res["index"]["live_merge"] is False and "НЕ учтены" in res["index"]["note"]


def test_bsl_sql_is_read_only_and_single_statement(ws: Workspace) -> None:
    """Индекс открывается только на чтение, и отказ обязан быть внятным, а не ошибкой SQLite."""
    fts.index_for(ws).build(wait=120)
    for bad in ("DROP TABLE symbols", "UPDATE symbols SET name='x'",
                "PRAGMA table_info(symbols)", "SELECT 1; SELECT 2",
                "WITH x AS (SELECT 1) DELETE FROM symbols"):
        out = fts.sql_query(ws, bad)
        assert "error" in out, f"пропущен запрос: {bad}"
        assert "rows" not in out
    # схема прилагается к отказу — агенту есть от чего оттолкнуться
    assert "schema" in fts.sql_query(ws, "DROP TABLE symbols")


def test_bsl_sql_truncation_is_declared(ws: Workspace) -> None:
    """Обрезка выдачи заявлена, и сказано, как узнать полный счёт (его НЕ выдумываем)."""
    fts.index_for(ws).build(wait=120)
    res = fts.sql_query(ws, "SELECT name FROM symbols", max_rows=1)
    assert res["row_count"] == 1
    assert res["truncated"] is True
    assert "COUNT(*)" in res["total_hint"]
    full = fts.sql_query(ws, "SELECT name FROM symbols", max_rows=500)
    assert full["truncated"] is False and full["total_hint"] is None
