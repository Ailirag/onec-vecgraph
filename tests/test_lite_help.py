"""Lite platform-help (.hbk): name index, docinfo/get_document/search semantics, admin wiring.

Контейнер подменяется на уровне `hbk_container.named_elements` (возвращаем настоящий ZIP
в памяти) — реальные `iter_html_pages`/чтение страниц работают без изменений, поэтому
тесты покрывают и «наш» код, и склейку с общим парсером. Реальные .hbk — skipif-тесты."""

from __future__ import annotations

import glob
import io
import zipfile
from pathlib import Path

import pytest

from onec_vecgraph.lite import admin as lite_admin
from onec_vecgraph.lite import platform_help as ph
from onec_vecgraph.lite import server as lite_server

_REAL_BINS = sorted(glob.glob(r"C:\Program Files\1cv8\*\bin\shcntx_ru.hbk"))


def _page(title: str, body: str) -> bytes:
    return (f"<html><head><meta charset='utf-8'></head><body><h1>{title}</h1>"
            f"<p>{body}</p></body></html>").encode("utf-8")


_PAGES = {
    "v27.hbk": [
        ("objects/arr.html", _page("Массив.Найти (Array.Find)", "Ищет значение в массиве.")),
        ("objects/vt.html", _page("ТаблицаЗначений.Найти (ValueTable.Find)", "Ищет в таблице значений.")),
        ("global/find.html", _page("Найти (Find)", "Глобальный поиск подстроки.")),
    ],
    "v18.hbk": [
        ("objects/arr.html", _page("Массив.Найти (Array.Find)", "Старая сборка справки.")),
    ],
}

_RESOLVED = {
    "v27": [("v27.hbk", "8.3.27.2130", "context")],
    "v18": [("v18.hbk", "8.3.18.1289", "context")],
}


def _fake_named_elements(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for zip_path, html in _PAGES[str(path)]:
            z.writestr(zip_path, html)
    return {"FileStorage": buf.getvalue()}


@pytest.fixture()
def catalog(monkeypatch: pytest.MonkeyPatch) -> ph.HelpCatalog:
    def fake_resolve(entry: dict):
        key = str(entry.get("path"))
        if key not in _RESOLVED:
            raise FileNotFoundError(f"нет такого пути: {key}")
        return _RESOLVED[key]

    monkeypatch.setattr(ph, "_resolve_files", fake_resolve)
    monkeypatch.setattr(ph.hbk_container, "named_elements", _fake_named_elements)
    cat = ph.HelpCatalog()
    errs = cat.configure([{"version": "", "path": "v27"}, {"version": "", "path": "v18"}])
    assert errs == []
    return cat


def test_parse_and_render_help_lines() -> None:
    text = 'C:\\Program Files\\1cv8\\8.3.27.2130\\bin\n8.3.18 = "D:\\help\\shcntx_ru.hbk";\n\n'
    entries = ph.parse_help_lines(text)
    assert entries == [
        {"version": "", "path": "C:\\Program Files\\1cv8\\8.3.27.2130\\bin"},
        {"version": "8.3.18", "path": "D:\\help\\shcntx_ru.hbk"},
    ]
    rendered = ph.render_help_lines(entries)
    assert ph.parse_help_lines(rendered) == entries


def test_pv_key_numeric_order() -> None:
    assert ph._pv_key("8.3.27.2130") < ph._pv_key("unknown")  # numeric builds first
    newest = sorted(["8.3.9.999", "8.3.27.2130"], key=lambda v: [-x for x in ph._pv_key(v)[1]])
    assert newest[0] == "8.3.27.2130"


def test_configure_keeps_valid_entries_and_reports_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(entry: dict):
        rows = _RESOLVED.get(str(entry.get("path")))
        if rows is None:
            raise FileNotFoundError("не найден")
        return rows

    monkeypatch.setattr(ph, "_resolve_files", resolve)
    cat = ph.HelpCatalog()
    errs = cat.configure([{"version": "", "path": "v27"}, {"version": "", "path": "bad"}])
    assert len(errs) == 1 and "bad" in errs[0]
    assert [e["path"] for e in cat.entries] == ["v27"]


def test_docinfo_exact_ambiguous_and_version(catalog: ph.HelpCatalog) -> None:
    # полное имя без версии: две сборки → дизамбигуация
    both = catalog.docinfo("Массив.Найти")
    assert both["ambiguous"] and len(both["candidates"]) == 2
    # с версией → полный текст темы
    doc = catalog.docinfo("Массив.Найти", platform_version="8.3.27.2130")
    assert doc["found"] and "Ищет значение в массиве" in doc["text"]
    assert doc["fqn"] == "platform_help:8.3.27.2130|Массив.Найти"
    assert doc["en_name"] == "Array.Find" and doc["help_kind"] == "context"
    # английское имя
    en = catalog.docinfo("array.find", platform_version="8.3.18.1289")
    assert en["found"] and "Старая сборка" in en["text"]
    # короткое имя «Найти» — неоднозначно (Массив/ТаблицаЗначений/глобальный)
    short = catalog.docinfo("Найти", platform_version="8.3.27.2130")
    assert short["ambiguous"] and len(short["candidates"]) == 3
    assert catalog.docinfo("НетТакого")["found"] is False


def test_get_document_newest_and_fqn_forms(catalog: ph.HelpCatalog) -> None:
    newest = catalog.get_document("Массив.Найти")
    assert newest["platform_version"] == "8.3.27.2130"  # без версии — самая свежая сборка
    old = catalog.get_document("platform_help:8.3.18.1289|Массив.Найти")
    assert old["found"] and "Старая сборка" in old["text"]
    assert catalog.get_document("Массив.Найти", "9.9.9.9")["found"] is False


def test_search_titles_rank_and_version_filter(catalog: ph.HelpCatalog) -> None:
    res = catalog.search_titles("найти")
    assert res["match_count"] == 4  # 3 темы v27 + 1 v18
    assert res["matches"][0]["title"] == "Найти"  # startswith ранжируется выше
    v18 = catalog.search_titles("найти", platform_version="8.3.18.1289")
    assert v18["match_count"] == 1


def test_versions_topics_after_index(catalog: ph.HelpCatalog) -> None:
    before = catalog.versions()
    assert before["indexed"] is False
    assert all(v["topics"] is None for v in before["versions"])
    catalog.index()
    after = catalog.versions()
    assert after["indexed"] is True
    by_pv = {v["platform_version"]: v for v in after["versions"]}
    assert by_pv["8.3.27.2130"]["topics"] == 3 and by_pv["8.3.18.1289"]["topics"] == 1
    assert [v["platform_version"] for v in after["versions"]][0] == "8.3.27.2130"  # свежие первыми


def test_state_persists_help_entries(tmp_path: Path) -> None:
    f = tmp_path / "cfg.json"
    lite_admin.save_paths(f, "H:\\ut", [], platform_help=[{"version": "", "path": "C:\\bin"}])
    assert lite_admin.load_help_entries(f) == [{"version": "", "path": "C:\\bin"}]
    # platform_help=None сохраняет уже записанное
    lite_admin.save_paths(f, "H:\\ut2", ["D:\\ext"])
    assert lite_admin.load_paths(f) == ("H:\\ut2", ["D:\\ext"])
    assert lite_admin.load_help_entries(f) == [{"version": "", "path": "C:\\bin"}]


def test_admin_apply_help_only_and_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "cfg.json"))
    monkeypatch.delenv("ONEC_LITE_ROOT", raising=False)
    monkeypatch.delenv("ONEC_LITE_HELP", raising=False)
    monkeypatch.setattr(lite_server, "_WS", None)
    monkeypatch.setattr(lite_server, "_HELP", ph.HelpCatalog())
    monkeypatch.setattr(lite_server, "_HELP_INIT", True)
    monkeypatch.setattr(ph, "_resolve_files", lambda e: _RESOLVED[str(e.get("path"))])
    monkeypatch.setattr(ph.hbk_container, "named_elements", _fake_named_elements)

    snap, err = lite_server.apply_admin_paths("", "", help_text="v27")
    assert err is None and snap is not None
    assert snap["configured"] is False  # рабочая копия не задана — это допустимо
    assert snap["platform_help"]["versions"][0]["platform_version"] == "8.3.27.2130"
    assert lite_admin.load_help_entries(lite_admin.state_file()) == [{"version": "", "path": "v27"}]

    doc = lite_server.platform_docinfo("Массив.Найти", platform_version="8.3.27.2130")
    assert doc["found"] and "Ищет значение" in doc["text"]
    vs = lite_server.platform_versions()
    assert vs["indexed"] is True and vs["versions"][0]["topics"] == 3

    # пусто и root, и справка → ошибка
    _s, err2 = lite_server.apply_admin_paths("", "", "")
    assert err2 is not None


def test_unconfigured_help_tools_answer_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lite_server, "_HELP", ph.HelpCatalog())
    monkeypatch.setattr(lite_server, "_HELP_INIT", True)
    assert "не настроена" in lite_server.platform_docinfo("Массив.Найти")["error"]
    assert lite_server.platform_versions()["count"] == 0


@pytest.mark.skipif(not _REAL_BINS, reason="no installed 1C platform help (.hbk) on this machine")
def test_real_hbk_index_and_document() -> None:
    bin_dir = str(Path(_REAL_BINS[-1]).parent)
    cat = ph.HelpCatalog()
    errs = cat.configure([{"version": "", "path": bin_dir, "limit": 20}])
    assert errs == []
    rows = cat.index()
    assert rows, "ожидались темы из реального shcntx/shlang"
    first = rows[0]
    assert first["platform_version"][0].isdigit()
    doc = cat.get_document(first["title"], first["platform_version"])
    assert doc["found"] and doc["text"]
