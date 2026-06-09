from onec_vecgraph.chunking import search_tokens
from onec_vecgraph.queries import _dedup, _fts_query, _rrf_fuse, _unit


def test_search_tokens_splits_camel_and_dotted_identifiers() -> None:
    toks = search_tokens("ОбщийМодуль.ПродажиТоваров").split()
    # originals kept, sub-words added, case-insensitively de-duplicated
    assert "ОбщийМодуль" in toks
    assert "Общий" in toks and "Модуль" in toks
    assert "ПродажиТоваров" in toks
    assert "Продажи" in toks and "Товаров" in toks


def test_search_tokens_dedup_preserves_order_and_ignores_empty() -> None:
    assert search_tokens("", None, "Контрагенты Контрагенты") == "Контрагенты"


def test_fts_query_tokenizes_and_drops_lucene_specials() -> None:
    q = _fts_query("Справочники.Контрагенты()")
    # identifiers split into sub-word tokens; no Lucene special chars survive
    assert "Контрагенты" in q
    assert "(" not in q and ")" not in q and "." not in q


def test_unit_keeps_routine_granularity_and_collapses_code_parts() -> None:
    code_row = {"fqn": "Catalog.X", "via": "code", "chunk_fqn": "Catalog.X.Module.ObjectModule::Провести#code"}
    code_part = {"fqn": "Catalog.X", "via": "code", "chunk_fqn": "Catalog.X.Module.ObjectModule::Провести#code/1"}
    obj_row = {"fqn": "Catalog.X", "via": "object", "chunk_fqn": "Catalog.X#object"}
    assert _unit(code_row) == "Catalog.X.Module.ObjectModule::Провести"
    assert _unit(code_part) == "Catalog.X.Module.ObjectModule::Провести"  # split parts collapse
    assert _unit(obj_row) == "Catalog.X"


def test_dedup_collapses_objects_and_code_parts_but_keeps_distinct_routines() -> None:
    rows = [
        {"fqn": "Catalog.X", "via": "object", "chunk_fqn": "Catalog.X#object"},
        {"fqn": "Catalog.X", "via": "attribute", "chunk_fqn": "Catalog.X.Attribute.A#attr"},
        {"fqn": "Catalog.X", "via": "code", "chunk_fqn": "Catalog.X.Module.M::A#code/0"},
        {"fqn": "Catalog.X", "via": "code", "chunk_fqn": "Catalog.X.Module.M::A#code/1"},
        {"fqn": "Catalog.X", "via": "code", "chunk_fqn": "Catalog.X.Module.M::B#code"},
    ]
    kept = _dedup(rows)
    units = [_unit(r) for r in kept]
    assert units == ["Catalog.X", "Catalog.X.Module.M::A", "Catalog.X.Module.M::B"]


def test_rrf_fuse_surfaces_routine_address_for_code_units() -> None:
    sem = [{"fqn": "Catalog.X", "kind": "Catalog", "via": "code",
            "chunk_fqn": "Catalog.X.Module.M::Провести#code/0", "chunk_name": "Провести", "matched": "..."}]
    fused = _rrf_fuse([("semantic", sem)], top_k=5)
    assert fused[0]["routine_fqn"] == "Catalog.X.Module.M::Провести"
    assert fused[0]["routine"] == "Провести"


def test_rrf_fuse_surfaces_corpus_from_source() -> None:
    sem = [{"fqn": "Document.X", "kind": "Document", "via": "its",
            "chunk_fqn": "its::a#0", "source": "its", "matched": "методика проведения"}]
    fused = _rrf_fuse([("semantic", sem)], top_k=5)
    assert fused[0]["corpus"] == "its"
