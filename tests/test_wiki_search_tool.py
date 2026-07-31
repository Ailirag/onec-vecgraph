"""Отдельный тул поиска по векторизованной вики.

Смысл отдельного тула — НАДЁЖНОСТЬ ВЫБОРА: модель заметно вернее берёт хорошо названный
инструмент, чем выставляет необязательный параметр `source` у общего поиска. Поэтому здесь
проверяется ровно то, что делает его отдельным: корпус прибит и не переопределяется, имя не
сталкивается с живым MCP Вики, а описание не выдаёт срез за живые страницы.
"""

from __future__ import annotations

from unittest import mock

from onec_vecgraph import server as vg_server
from onec_vecgraph.sources.wiki import WikiSource


class TestCorpusIsPinned:
    def test_corpus_tag_comes_from_the_adapter(self):
        # Строкой корпус задавать нельзя: переименование в адаптере тихо превратило бы
        # поиск в обращение к пустоте.
        assert vg_server.WIKI_CORPUS == WikiSource.source == "wiki"

    def test_search_is_restricted_to_the_wiki_corpus(self):
        captured = {}

        def fake_hybrid(store, tenant, query, embedder, top_k, **kwargs):
            captured.update(kwargs)
            captured["tenant"] = tenant
            captured["query"] = query
            captured["top_k"] = top_k
            return {"hits": []}

        with mock.patch.object(vg_server.queries, "hybrid_search", fake_hybrid), \
             mock.patch.object(vg_server, "Neo4jStore") as store_cls, \
             mock.patch("onec_vecgraph.embeddings.runtime.provider", return_value=object()), \
             mock.patch("onec_vecgraph.embeddings.runtime.reranker", return_value=None), \
             mock.patch.object(vg_server, "_tenant", return_value="ut"), \
             mock.patch.object(vg_server, "_shared", return_value="__shared__"):
            store_cls.from_settings.return_value.__enter__.return_value = object()
            vg_server.wiki_semantic_search(None, "как считается себестоимость", top_k=5)

        assert captured["source"] == ["wiki"], "корпус обязан быть прибит к wiki"
        assert captured["tenant"] == "ut"
        assert captured["shared_tenant_id"] == "__shared__", \
            "общедоступная часть вики читается аддитивно из общего тенанта"
        assert captured["top_k"] == 5

    def test_tool_has_no_source_parameter_to_override(self):
        # Если бы `source` остался в сигнатуре, модель могла бы им переопределить корпус —
        # и отдельный тул перестал бы что-либо гарантировать.
        import inspect

        params = set(inspect.signature(vg_server.wiki_semantic_search).parameters)
        assert "source" not in params


class TestNaming:
    def test_name_does_not_collide_with_the_live_wiki_mcp(self):
        # У живого MCP Яндекс Вики есть wiki_search (полнотекстовый по страницам). Если оба
        # эндпоинта выданы одному диалогу, одинаковые имена схлопнутся при маршрутизации —
        # тул вроде есть, а работает не тот.
        assert vg_server.wiki_semantic_search.__name__ == "wiki_semantic_search"
        assert vg_server.wiki_semantic_search.__name__ != "wiki_search"

    def test_description_warns_it_is_a_snapshot(self):
        # Выдать индекс за живые страницы — та же тихая неправда, что и устаревшая справка.
        text = (vg_server.wiki_semantic_search.__doc__ or "").lower()
        assert "не живая" in text or "не живую" in text or "срез" in text
        assert "wiki_get_page" in text, "должен подсказывать, чем взять свежую страницу"
