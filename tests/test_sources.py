import json

from onec_vecgraph import chunking
from onec_vecgraph.sources.base import owner_fqn, sha1_text
from onec_vecgraph.sources.git_artifacts import GitArtifactsSource
from onec_vecgraph.sources.its import ItsSource
from onec_vecgraph.sources.linking import extract_fqn_mentions
from onec_vecgraph.sources.manifest import load_manifest
from onec_vecgraph.sources.markdown import split_markdown_sections


def test_split_markdown_sections_tracks_heading_path() -> None:
    md = "preamble\n# A\natext\n## A1\na1text\n# B\nbtext"
    secs = split_markdown_sections(md)
    titles = [(s["title"], s["path"]) for s in secs]
    assert ("", []) in titles                       # preamble
    assert ("A", []) in titles
    assert ("A1", ["A"]) in titles                  # nested under A
    assert ("B", []) in titles
    a1 = next(s for s in secs if s["title"] == "A1")
    assert a1["body"] == "a1text"


def test_extract_fqn_mentions() -> None:
    text = "См. Document.РеализацияТоваров и Catalog.Контрагенты; ОбычныйТекст не fqn."
    assert extract_fqn_mentions(text) == {"Document.РеализацияТоваров", "Catalog.Контрагенты"}


def test_its_source_reads_json_units(tmp_path) -> None:
    (tmp_path / "a.json").write_text(json.dumps({
        "id": "art-1", "title": "Проведение", "section_path": ["Документы"],
        "text": "Как проводить документ.", "version_hash": "v1",
        "related_fqns": ["Document.РеализацияТоваров"], "source_url": "its://a",
    }), encoding="utf-8")
    units = list(ItsSource({"path": str(tmp_path)}).units())
    assert len(units) == 1
    u = units[0]
    assert u.external_id == "art-1" and u.title == "Проведение"
    assert u.links == ["Document.РеализацияТоваров"]
    assert owner_fqn("its", u.external_id) == "its:art-1"


def test_its_source_synthesizes_id_and_version_when_missing(tmp_path) -> None:
    (tmp_path / "b.json").write_text(json.dumps({"text": "Текст без id."}), encoding="utf-8")
    u = next(iter(ItsSource({"path": str(tmp_path)}).units()))
    assert u.external_id and u.version_hash == sha1_text("Текст без id.")


def test_git_artifacts_source_sections_per_file(tmp_path) -> None:
    (tmp_path / "design.md").write_text("# Цель\nОписание. Document.ЗаказКлиента\n# Решение\nДетали.",
                                        encoding="utf-8")
    units = list(GitArtifactsSource({"path": str(tmp_path)}).units())
    titles = {u.title for u in units}
    assert {"Цель", "Решение"} <= titles
    goal = next(u for u in units if u.title == "Цель")
    assert goal.section_path[0] == "design.md"
    assert goal.version_hash  # content-hashed


def test_its_source_sets_doc_topic_and_corpus_version(tmp_path) -> None:
    (tmp_path / "a.json").write_text(json.dumps({"text": "Текст.", "title": "T"}), encoding="utf-8")
    u = next(iter(ItsSource({"path": str(tmp_path), "corpus_version": "config:ERP_2.5"}).units()))
    assert u.extra["doc_topic"] == "config"            # ITS defaults to configuration topic
    assert u.extra["corpus_version"] == "config:ERP_2.5"


def test_its_source_record_overrides_topic(tmp_path) -> None:
    (tmp_path / "a.json").write_text(json.dumps({"text": "T", "doc_topic": "platform"}), encoding="utf-8")
    u = next(iter(ItsSource({"path": str(tmp_path)}).units()))
    assert u.extra["doc_topic"] == "platform"          # per-record override beats the manifest default


def test_git_artifacts_source_sets_task_topic(tmp_path) -> None:
    (tmp_path / "d.md").write_text("# A\nbody", encoding="utf-8")
    u = next(iter(GitArtifactsSource({"path": str(tmp_path)}).units()))
    assert u.extra["doc_topic"] == "task"


def test_load_manifest_json(tmp_path) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"tenant": "demo", "sources": [{"type": "its", "path": "/x"}]}), encoding="utf-8")
    data = load_manifest(p)
    assert data["tenant"] == "demo" and data["sources"][0]["type"] == "its"


def test_doc_chunks_builds_breadcrumb_and_source() -> None:
    cs = chunking.doc_chunks("Проведение", "тело раздела", source="its",
                             owner_fqn="its:art-1", section_path=["Документы"])
    assert len(cs) == 1
    c = cs[0]
    assert c.chunk_kind == "its" and c.source == "its" and c.owner_fqn == "its:art-1"
    assert c.fqn == "its:art-1#chunk"
    assert "Проведение ▸ Документы" in c.text
