import glob

import pytest

from onec_vecgraph.sources.hbk import HbkSource, _help_kind, _parse_page, _version_from_path

_REAL = sorted(glob.glob(r"C:\Program Files\1cv8\*\bin\shcntx_ru.hbk"))


def test_version_from_path() -> None:
    assert _version_from_path(r"C:\Program Files\1cv8\8.3.27.1989\bin\shcntx_ru.hbk") == "8.3.27.1989"
    assert _version_from_path("/help/shcntx_ru.hbk") is None


def test_help_kind_from_filename() -> None:
    assert _help_kind("/x/shcntx_ru.hbk") == "context"
    assert _help_kind("/x/shlang_ru.hbk") == "language"
    assert _help_kind("/x/shquery_ru.hbk") == "query"


def test_parse_page_splits_ru_en_name_and_text() -> None:
    html = ("<html><head><meta charset='utf-8'></head><body>"
            "<h1>Массив.Найти (Array.Find)</h1><p>Ищет значение в массиве.</p></body></html>").encode("utf-8")
    ru, en, text = _parse_page(html)
    assert ru == "Массив.Найти" and en == "Array.Find"
    assert "Ищет значение в массиве." in text


def test_parse_page_no_parens() -> None:
    ru, en, _ = _parse_page(b"<html><body><h1>Prikladnye obyekty</h1><p>x</p></body></html>")
    assert ru == "Prikladnye obyekty" and en is None


@pytest.mark.skipif(not _REAL, reason="no installed 1C platform help (.hbk) on this machine")
def test_hbk_source_units_on_real_file() -> None:
    bin_dir = _REAL[-1].rsplit("\\", 1)[0]
    src = HbkSource({"bin": bin_dir, "domains": ["shcntx"], "limit": 40})
    units = list(src.units())
    assert units, "expected DocUnits from a real shcntx_ru.hbk"
    u = units[0]
    assert u.extra["platform_version"] and u.extra["platform_version"][0].isdigit()
    assert u.extra["help_kind"] == "context"
    assert u.extra["full_name_norm"] == u.extra["full_name_norm"].lower()
    assert u.external_id.startswith(u.extra["platform_version"] + "|")
    assert u.text  # HTML decoded to plain text
