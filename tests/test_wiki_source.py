"""Корпус `wiki`: адаптер раздела Яндекс Вики и дешёвый инкремент в движке.

Главный риск здесь — не падение, а ЛИШНЯЯ РАБОТА и ТИХАЯ ПОТЕРЯ: холостой прогон, который
всё равно качает весь раздел, либо неизменившиеся страницы, сочтённые удалёнными. Оба отказа
незаметны по результату, поэтому проверяются счётчиками вызовов, а не только выдачей.

Сеть не используется: клиент подставляется через шов конструктора.
"""

from __future__ import annotations

import pytest

from onec_vecgraph.sources.base import DocUnit
from onec_vecgraph.sources.wiki import WikiSource, effective_access
from onec_vecgraph.sources.wiki_client import WikiError, normalize_slug

ROOT = "homepage/gt-products/1s"
OPEN_ACCESS = {"access_type": "inherited", "inherited_access_type": "all_staff"}
CLOSED_ACCESS = {"access_type": "restricted"}


def _page(page_id, slug, title, modified_at, content="текст", access=None, crumbs=None):
    return {
        "id": page_id,
        "slug": slug,
        "title": title,
        "page_type": "wysiwyg",
        "attributes": {"modified_at": modified_at, "created_at": modified_at},
        "access_policy": access if access is not None else OPEN_ACCESS,
        "content": content,
        "breadcrumbs": crumbs or [],
    }


class FakeWikiClient:
    """Клиент без сети. Считает обращения, чтобы можно было проверить, что лишнего не качаем."""

    BASE_FIELDS = ("id", "slug", "title", "page_type")

    def __init__(self, pages: dict[str, dict], tree: list[dict], transient: set[str] | None = None):
        self.pages = pages
        self.tree = tree
        # Страницы, которые «не отвечают» (троттлинг) — в отличие от отсутствующих (404).
        self.transient = transient or set()
        self.attribute_fetches: list[str] = []
        self.content_fetches: list[str] = []

    def _project(self, data: dict, fields: tuple[str, ...]) -> dict:
        return {k: v for k, v in data.items() if k in self.BASE_FIELDS or k in fields}

    def page(self, slug: str, fields: tuple[str, ...] = ()) -> dict:
        data = self.pages.get(slug)
        if data is None:
            raise WikiError(f"нет страницы {slug}")
        return self._project(data, fields)

    def descendants(self, page_id, page_size: int = 100) -> list[dict]:
        return list(self.tree)

    def pages_bulk(self, slugs, fields):
        for slug in slugs:
            if "content" in fields:
                self.content_fetches.append(slug)
            else:
                self.attribute_fetches.append(slug)
            if slug in self.transient:
                yield slug, None, WikiError(f"{slug}: HTTP 429", 429)
                continue
            data = self.pages.get(slug)
            if data is None:
                yield slug, None, WikiError(f"{slug}: HTTP 404", 404)
                continue
            yield slug, self._project(data, fields), None


def _source(pages, tree, **entry):
    base = {"type": "wiki", "root": ROOT}
    base.update(entry)
    return WikiSource(base, client=FakeWikiClient(pages, tree))


def _fixture():
    pages = {
        ROOT: _page(100, ROOT, "1С", "2026-07-01T00:00:00Z", content="корень"),
        f"{ROOT}/a": _page(201, f"{ROOT}/a", "Раздел A", "2026-07-02T00:00:00Z",
                           crumbs=[{"title": "Вики"}, {"title": "1С"}, {"title": "Раздел A"}]),
        f"{ROOT}/a/b": _page(202, f"{ROOT}/a/b", "Страница B", "2026-07-03T00:00:00Z"),
    }
    tree = [{"id": 201, "slug": f"{ROOT}/a"}, {"id": 202, "slug": f"{ROOT}/a/b"}]
    return pages, tree


class TestSlugNormalisation:
    def test_url_and_slug_are_equivalent(self):
        assert normalize_slug("https://wiki.yandex.ru/homepage/gt-products/1s/") == ROOT
        assert normalize_slug("/homepage/gt-products/1s") == ROOT
        assert normalize_slug(ROOT) == ROOT

    def test_root_is_required(self):
        with pytest.raises(ValueError):
            WikiSource({"type": "wiki"}, client=object())


class TestVersions:
    def test_versions_cover_the_whole_subtree_without_reading_content(self):
        pages, tree = _fixture()
        src = _source(pages, tree)
        versions = src.versions()
        # Корень + два потомка; ключ — ИДЕНТИФИКАТОР страницы, а не slug.
        assert set(versions) == {"100", "201", "202"}
        # Содержимое при этом не запрашивалось ни разу — в этом весь смысл дешёвой проверки.
        assert src.client.content_fetches == []
        assert len(src.client.attribute_fetches) == 3

    def test_version_changes_only_with_modified_at(self):
        pages, tree = _fixture()
        first = _source(pages, tree).versions()
        pages[f"{ROOT}/a"]["title"] = "Раздел A переименован"
        pages[f"{ROOT}/a"]["content"] = "другой текст"
        same = _source(pages, tree).versions()
        assert same["201"] == first["201"], "правка без смены modified_at не должна менять версию"
        pages[f"{ROOT}/a"]["attributes"]["modified_at"] = "2026-07-09T00:00:00Z"
        changed = _source(pages, tree).versions()
        assert changed["201"] != first["201"]

    def test_include_root_false_skips_the_section_page(self):
        pages, tree = _fixture()
        src = _source(pages, tree, include_root=False)
        assert set(src.versions()) == {"201", "202"}


class TestAccessFilter:
    def test_public_corpus_excludes_restricted_pages(self):
        # Общедоступный корпус живёт в ОБЩЕМ тенанте: закрытая страница, попав в него, стала бы
        # видна всем проектам в обход поимённых списков доступа Вики.
        pages, tree = _fixture()
        pages[f"{ROOT}/a/b"]["access_policy"] = CLOSED_ACCESS
        src = _source(pages, tree)
        assert "202" not in src.versions()
        assert src.skipped_by_access == [f"{ROOT}/a/b"]
        assert [u.external_id for u in src.units()] == ["100", "201"]

    def test_restricted_corpus_takes_exactly_the_complement(self):
        pages, tree = _fixture()
        pages[f"{ROOT}/a/b"]["access_policy"] = CLOSED_ACCESS
        src = _source(pages, tree, access_mode="restricted")
        assert set(src.versions()) == {"202"}

    def test_public_and_restricted_together_cover_everything(self):
        # Ключевое свойство: щелей между корпусами нет, ни одна страница не теряется молча.
        pages, tree = _fixture()
        pages[f"{ROOT}/a"]["access_policy"] = {"access_type": "что-то новое"}
        pages[f"{ROOT}/a/b"]["access_policy"] = CLOSED_ACCESS
        public = set(_source(pages, tree, access_mode="public").versions())
        restricted = set(_source(pages, tree, access_mode="restricted").versions())
        every = set(_source(pages, tree, access_mode="all").versions())
        assert public & restricted == set()
        assert public | restricted == every == {"100", "201", "202"}

    def test_unknown_access_value_goes_to_the_restricted_side(self):
        # Незнакомый уровень доступа трактуется как закрытый: ошибиться в эту сторону безопасно.
        pages, tree = _fixture()
        pages[f"{ROOT}/a"]["access_policy"] = {"access_type": "что-то новое"}
        assert "201" not in _source(pages, tree, access_mode="public").versions()
        assert "201" in _source(pages, tree, access_mode="restricted").versions()

    def test_effective_access_resolves_inheritance(self):
        assert effective_access(OPEN_ACCESS) == "all_staff"
        assert effective_access({"access_type": "restricted"}) == "restricted"
        assert effective_access(None) == ""

    def test_bad_access_mode_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="access_mode"):
            WikiSource({"type": "wiki", "root": ROOT, "access_mode": "нет-такого"}, client=object())


class TestUnits:
    def test_restrict_to_loads_only_the_requested_pages(self):
        pages, tree = _fixture()
        src = _source(pages, tree)
        src.versions()
        src.restrict_to({"202"})
        units = list(src.units())
        assert [u.external_id for u in units] == ["202"]
        assert src.client.content_fetches == [f"{ROOT}/a/b"]

    def test_unit_carries_breadcrumbs_without_the_page_itself(self):
        pages, tree = _fixture()
        src = _source(pages, tree)
        unit = next(u for u in src.units() if u.external_id == "201")
        # Последний элемент крошек — сама страница, её заголовок идёт отдельным полем.
        assert unit.section_path == ["Вики", "1С"]
        assert unit.title == "Раздел A"
        assert unit.source_url == f"https://wiki.yandex.ru/{ROOT}/a/"
        assert unit.extra["wiki_slug"] == f"{ROOT}/a"
        assert unit.extra["modified_at"] == "2026-07-02T00:00:00Z"

    def test_long_sections_keep_their_own_heading_path(self):
        # Ради этого разбиения и заведено поле sections: чанк из середины длинной страницы
        # должен нести свой подраздел, а не только положение страницы в дереве Вики.
        # Разделы намеренно длинные — короткие сливаются, см. следующий тест.
        pages, tree = _fixture()
        long_body = "строка текста " * 120  # заведомо больше бюджета слияния
        pages[f"{ROOT}/a"]["content"] = (
            f"преамбула {long_body}\n\n# Установка\n{long_body}\n\n"
            f"## Права\n{long_body}\n\n# Откат\n{long_body}")
        unit = next(u for u in _source(pages, tree).units() if u.external_id == "201")
        paths = [s["path"] for s in unit.sections]
        assert [] in paths, "преамбула до первого заголовка не должна теряться"
        assert ["Установка"] in paths
        assert ["Установка", "Права"] in paths, "вложенный заголовок несёт путь предка"
        assert ["Откат"] in paths

    def test_short_sections_are_merged_to_avoid_tiny_chunks(self):
        # Замер на живом разделе: без слияния медиана чанка ~260 непробельных символов —
        # заголовок и пара строк. Такие огрызки ухудшают поиск, а не улучшают.
        pages, tree = _fixture()
        pages[f"{ROOT}/a"]["content"] = (
            "# Один\nкоротко\n\n# Два\nтоже коротко\n\n# Три\nи это")
        unit = next(u for u in _source(pages, tree).units() if u.external_id == "201")
        assert unit.sections == [], "три коротких раздела схлопываются в один — делить нечего"

    def test_merged_section_claims_only_the_common_ancestor(self):
        # Слитый чанк не должен выдавать себя за один конкретный подраздел.
        pages, tree = _fixture()
        long_body = "строка текста " * 120
        pages[f"{ROOT}/a"]["content"] = (
            f"# Установка\n## Шаг один\nкоротко\n\n## Шаг два\nтоже коротко\n\n# Откат\n{long_body}")
        unit = next(u for u in _source(pages, tree).units() if u.external_id == "201")
        merged = unit.sections[0]
        assert merged["path"] == ["Установка"], f"ожидался общий предок, получено {merged['path']}"
        assert "Шаг один" in merged["body"] and "Шаг два" in merged["body"], \
            "заголовки слитых разделов обязаны остаться в тексте"

    def test_page_without_headings_keeps_plain_chunking(self):
        pages, tree = _fixture()
        pages[f"{ROOT}/a"]["content"] = "просто текст без заголовков"
        unit = next(u for u in _source(pages, tree).units() if u.external_id == "201")
        assert unit.sections == [], "без заголовков режем по размеру, как раньше"

    def test_empty_page_yields_nothing(self):
        pages, tree = _fixture()
        pages[f"{ROOT}/a"]["content"] = "   "
        assert "201" not in [u.external_id for u in _source(pages, tree).units()]

    def test_deleted_page_is_dropped_from_the_catalog(self):
        # 404 = страницы правда нет. Единственный случай, когда пропуск законен: движок
        # удалит её из корпуса, и это верно.
        pages, tree = _fixture()
        tree.append({"id": 999, "slug": f"{ROOT}/удалена"})
        src = _source(pages, tree)
        assert set(src.versions()) == {"100", "201", "202"}
        assert src.vanished == [f"{ROOT}/удалена"]
        assert src.report()["unreadable"] == 0

    def test_throttled_page_aborts_the_crawl_instead_of_shrinking_the_corpus(self):
        # Ключевая защита. Страница, выпавшая из versions(), для движка неотличима от
        # удалённой — он сотрёт её вместе с чанками. Троттлинг API не должен выкашивать раздел.
        pages, tree = _fixture()
        src = WikiSource({"type": "wiki", "root": ROOT},
                         client=FakeWikiClient(pages, tree, transient={f"{ROOT}/a/b"}))
        with pytest.raises(WikiError, match="неполон"):
            src.versions()

    def test_throttled_page_aborts_content_load_instead_of_keeping_stale_text(self):
        # Обратная опасность: пропустить изменившуюся страницу — значит оставить в корпусе
        # её прежнюю редакцию под видом свежей.
        pages, tree = _fixture()
        client = FakeWikiClient(pages, tree)
        src = WikiSource({"type": "wiki", "root": ROOT}, client=client)
        src.versions()
        client.transient = {f"{ROOT}/a/b"}
        src.restrict_to({"202"})
        with pytest.raises(WikiError, match="устаревшая редакция"):
            list(src.units())


class FakeEmbedder:
    """Эмбеддер не вызывается (_embed_and_write подменён), но его dim читает создание индекса."""

    dim = 1024


class FakeStore:
    """Минимальное хранилище: только то, чем пользуется ingest_source."""

    def __init__(self, versions: dict[str, str] | None = None):
        self.versions = dict(versions or {})
        self.deleted_docs: list[str] = []
        self.deleted_sources: list[str] = []
        self.written: list[dict] = []

    def doc_versions(self, tenant_id, source):
        return dict(self.versions)

    def delete_source(self, tenant_id, source):
        self.deleted_sources.append(source)
        self.versions.clear()

    def delete_docs(self, tenant_id, fqns):
        self.deleted_docs.extend(fqns)

    def write_documents(self, tenant_id, owner_label, rows):
        self.written.extend(rows)

    def create_vector_index(self, *a, **k):
        pass

    def create_fulltext_index(self, *a, **k):
        pass


@pytest.fixture()
def ingest_module(monkeypatch):
    from onec_vecgraph import ingest

    embedded: list[list] = []

    def fake_embed(store, tenant_id, embedder, chunks, owner_label="", label=""):
        embedded.append(list(chunks))
        return len(chunks), {}, {}

    monkeypatch.setattr(ingest, "_embed_and_write", fake_embed)
    monkeypatch.setattr(ingest, "link_mentions", lambda *a, **k: 0)
    ingest._test_embedded = embedded  # type: ignore[attr-defined]
    return ingest


class SourceWithVersions:
    name = source = "wiki"
    owner_label = "Document"

    def __init__(self, declared: dict[str, str], texts: dict[str, str]):
        self.declared = declared
        self.texts = texts
        self.only: set[str] | None = None
        self.loaded: list[str] = []

    def versions(self):
        return dict(self.declared)

    def restrict_to(self, external_ids):
        self.only = set(external_ids)

    def units(self):
        wanted = self.declared if self.only is None else self.only
        for ext in sorted(wanted):
            self.loaded.append(ext)
            yield DocUnit(external_id=ext, title=f"стр {ext}", text=self.texts[ext],
                          version_hash=self.declared[ext])


class TestSectionChunking:
    def test_sections_produce_unique_chunk_fqns(self, ingest_module):
        # Каждый раздел режется отдельным вызовом doc_chunks, и без разведения пространства
        # имён нумерация в каждом начиналась бы с нуля — чанки затирали бы друг друга.
        store = FakeStore({})
        unit = DocUnit(
            external_id="1", title="Стр", text="неважно", version_hash="v1",
            section_path=["Вики"],
            sections=[{"path": ["Установка"], "body": "шаг один"},
                      {"path": ["Откат"], "body": "как вернуть"}],
        )

        class OneUnit:
            name = source = "wiki"
            owner_label = "Document"

            def versions(self):
                return None

            def restrict_to(self, ids):
                pass

            def units(self):
                yield unit

        ingest_module.ingest_source(store, "t", object(), OneUnit(), FakeEmbedder())
        chunks = ingest_module._test_embedded[-1]
        fqns = [c.fqn for c in chunks]
        assert len(fqns) == len(set(fqns)), f"fqn чанков столкнулись: {fqns}"
        # И в тексте чанка виден путь заголовка, а не только путь страницы.
        assert any("Установка" in c.text for c in chunks)
        assert any("Откат" in c.text for c in chunks)


class TestIncrementalEngine:
    def test_unchanged_pages_are_neither_reloaded_nor_deleted(self, ingest_module):
        # Ровно тот отказ, ради которого хук и делался: холостой прогон не должен ни качать
        # содержимое, ни считать неизменившиеся страницы удалёнными.
        declared = {"1": "v1", "2": "v2"}
        store = FakeStore({"wiki:1": "v1", "wiki:2": "v2"})
        src = SourceWithVersions(declared, {"1": "текст 1", "2": "текст 2"})
        result = ingest_module.ingest_source(store, "t", object(), src, FakeEmbedder())
        assert src.loaded == [], "содержимое неизменившихся страниц загружаться не должно"
        assert result["changed"] == 0
        assert result["deleted"] == 0
        assert store.deleted_docs == []
        assert result["units"] == 2

    def test_only_the_changed_page_is_loaded_and_written(self, ingest_module):
        declared = {"1": "v1", "2": "v2-новая"}
        store = FakeStore({"wiki:1": "v1", "wiki:2": "v2"})
        src = SourceWithVersions(declared, {"1": "текст 1", "2": "текст 2"})
        result = ingest_module.ingest_source(store, "t", object(), src, FakeEmbedder())
        assert src.loaded == ["2"]
        assert result["changed"] == 1
        assert [row["fqn"] for row in store.written] == ["wiki:2"]

    def test_page_gone_from_the_source_is_deleted(self, ingest_module):
        declared = {"1": "v1"}
        store = FakeStore({"wiki:1": "v1", "wiki:2": "v2"})
        src = SourceWithVersions(declared, {"1": "текст 1"})
        result = ingest_module.ingest_source(store, "t", object(), src, FakeEmbedder())
        assert result["deleted"] == 1
        assert "wiki:2" in store.deleted_docs

    def test_reset_ignores_declared_versions_and_reloads_everything(self, ingest_module):
        declared = {"1": "v1", "2": "v2"}
        store = FakeStore({"wiki:1": "v1", "wiki:2": "v2"})
        src = SourceWithVersions(declared, {"1": "текст 1", "2": "текст 2"})
        result = ingest_module.ingest_source(store, "t", object(), src, FakeEmbedder(), reset=True)
        assert src.loaded == ["1", "2"], "при reset сравнивать не с чем — грузим всё"
        assert store.deleted_sources == ["wiki"]
        assert result["changed"] == 2

    def test_source_without_versions_keeps_the_old_path(self, ingest_module):
        # Файловые корпуса (its/git_artifacts/hbk) не переопределяют versions и обязаны
        # продолжать работать по-прежнему.
        from onec_vecgraph.sources.base import Source

        class PlainSource(Source):
            name = source = "its"
            owner_label = "Document"

            def units(self):
                yield DocUnit(external_id="a", title="A", text="текст", version_hash="h1")

        store = FakeStore({})
        result = ingest_module.ingest_source(store, "t", object(), PlainSource(), FakeEmbedder())
        assert result["changed"] == 1
        assert result["units"] == 1
