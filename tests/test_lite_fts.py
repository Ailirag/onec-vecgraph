"""Lite FTS5 index: BM25 ranking, CamelCase tokens, Cyrillic endings, mtime increments.

БД кладётся во временный state-каталог (ONEC_LITE_STATE), не в рабочую копию."""

from __future__ import annotations

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
