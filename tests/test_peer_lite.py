"""Peer-lite deployment: tool-publication control + explicit not-vectorized search status.

When onec-vecgraph runs alongside an onec-lite MCP, PEER_LITE=true unpublishes the
structural tools lite already serves (live working copy), and search tools answer with a
machine-checkable status instead of a silently-empty result while a tenant awaits
vectorization. Standalone behaviour (PEER_LITE unset) must stay exactly as before.
"""

from __future__ import annotations

import importlib

import pytest
from neo4j.exceptions import ClientError

from onec_vecgraph import config, queries, server
from onec_vecgraph.config import Settings

# Unique-to-vecgraph tools that must stay published in peer-lite mode.
_PEER_KEPT = {
    "ping", "neo4j_health", "whoami", "list_configurations",
    "semantic_search", "hybrid_search", "dev_standards_search", "dev_standards_get",
    "its_find_related_docs", "its_get_document",
    "artifact_find_related_docs", "artifact_get_document",
    "impact_analysis", "call_path",
}


# ── publication gate (pure logic) ─────────────────────────────────────

def test_default_mode_publishes_everything() -> None:
    s = Settings(peer_lite=False)
    for name in (*server.LITE_COVERED_TOOLS, *_PEER_KEPT):
        assert server._published(name, s), name


def test_peer_lite_hides_exactly_the_lite_covered_set() -> None:
    s = Settings(peer_lite=True)
    for name in server.LITE_COVERED_TOOLS:
        assert not server._published(name, s), name
    for name in _PEER_KEPT:
        assert server._published(name, s), name


def test_peer_lite_keep_republishes_named_tools() -> None:
    s = Settings(peer_lite=True, peer_lite_keep="metrics, get_routine_source")
    assert server._published("metrics", s)
    assert server._published("get_routine_source", s)
    assert not server._published("get_object", s)


def test_disabled_tools_hide_in_any_mode_and_beat_keep() -> None:
    assert not server._published("call_path", Settings(disabled_tools="call_path"))
    s = Settings(peer_lite=True, peer_lite_keep="metrics", disabled_tools="metrics")
    assert not server._published("metrics", s)


def test_lite_covered_names_match_real_server_functions() -> None:
    # Guard against silent drift if a tool is renamed: every name in the set must still
    # be a function defined in server.py.
    for name in server.LITE_COVERED_TOOLS:
        assert callable(getattr(server, name)), name


# ── actual registration (module reload under PEER_LITE) ──────────────

def _registered() -> set[str]:
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def test_peer_lite_env_unpublishes_structural_tools(monkeypatch) -> None:
    monkeypatch.setenv("PEER_LITE", "true")
    config.get_settings.cache_clear()
    try:
        importlib.reload(server)
        names = _registered()
        assert names.isdisjoint(server.LITE_COVERED_TOOLS)
        assert _PEER_KEPT <= names
        # peer variant of the instructions is served on initialize
        assert "onec-lite" in (server.mcp.instructions or "")
    finally:
        monkeypatch.delenv("PEER_LITE", raising=False)
        config.get_settings.cache_clear()
        importlib.reload(server)
    # standalone restored: the full toolset is registered again
    assert server.LITE_COVERED_TOOLS <= _registered()
    assert _PEER_KEPT <= _registered()


def test_standalone_registration_includes_full_toolset() -> None:
    assert server.LITE_COVERED_TOOLS <= _registered()
    assert _PEER_KEPT <= _registered()


# ── explicit not-vectorized search status ─────────────────────────────

class _Embedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts, is_query=False):
        self.calls += 1
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


class _EmptyTenantStore:
    """Caller tenant has no chunks; the shared tenant optionally has some."""

    def __init__(self, shared_has: bool = False) -> None:
        self.shared_has = shared_has

    def has_chunks(self, tenant_id, chunk_kind=None):
        return tenant_id == "__shared__" and self.shared_has

    def filtered_chunk_count(self, *a, **k):
        return 0

    def vector_search(self, *a, **k):
        return []

    def exact_vector_search(self, *a, **k):
        return []

    def fulltext_search(self, *a, **k):
        return []


def test_semantic_search_reports_not_vectorized_without_embedding() -> None:
    emb = _Embedder()
    out = queries.semantic_search(_EmptyTenantStore(), "acme", "прибыль", emb)
    assert out["status"] == "not_vectorized"
    assert out["tenant_vectorized"] is False
    assert out["results"] == []
    assert "acme" in out["message"] and "vectorize" in out["message"]
    assert emb.calls == 0  # answered before loading/using the embedding model


def test_hybrid_search_reports_not_vectorized_without_shared() -> None:
    out = queries.hybrid_search(_EmptyTenantStore(), "acme", "прибыль", _Embedder())
    assert out["status"] == "not_vectorized" and out["results"] == []


def test_search_over_shared_corpora_is_annotated_not_blocked() -> None:
    out = queries.hybrid_search(_EmptyTenantStore(shared_has=True), "acme", "стандарт",
                                _Embedder(), shared_tenant_id="__shared__")
    assert out["status"] == "shared_corpora_only"  # the search DID run (against shared)
    assert out["tenant_vectorized"] is False
    assert out["results"] == []


def test_vectorized_tenant_response_shape_unchanged() -> None:
    class _Vectorized(_EmptyTenantStore):
        def has_chunks(self, tenant_id, chunk_kind=None):
            return True

    out = queries.hybrid_search(_Vectorized(), "acme", "прибыль", _Embedder())
    assert "status" not in out and "tenant_vectorized" not in out
    assert out == {"query": "прибыль", "mode": "hybrid", "results": []}


class _NoIndexStore(_EmptyTenantStore):
    """Chunks exist but the search index was never created (fresh DB)."""

    def has_chunks(self, tenant_id, chunk_kind=None):
        return True

    def vector_search(self, *a, **k):
        raise ClientError("There is no such vector schema index: chunk_embedding")


def test_missing_search_index_maps_to_not_vectorized_status() -> None:
    out = queries.semantic_search(_NoIndexStore(), "acme", "x", _Embedder())
    assert out["status"] == "not_vectorized"
    assert "index" in out["message"]


def test_missing_fulltext_index_in_hybrid_maps_to_status() -> None:
    class _NoFts(_NoIndexStore):
        def vector_search(self, *a, **k):
            return []

        def fulltext_search(self, *a, **k):
            raise ClientError("There is no such fulltext schema index: chunk_text")

    out = queries.hybrid_search(_NoFts(), "acme", "x", _Embedder())
    assert out["status"] == "not_vectorized"


def test_unrelated_client_errors_still_raise() -> None:
    class _Broken(_NoIndexStore):
        def vector_search(self, *a, **k):
            raise ClientError("The allocation of an extra 2.0 MiB would use more than the limit")

    with pytest.raises(ClientError):
        queries.semantic_search(_Broken(), "acme", "x", _Embedder())


# ── tenant_layers readiness snapshot ──────────────────────────────────

class _LayersStore:
    def __init__(self, objects=0, routines=0, by_kind=None):
        self.objects, self.routines = objects, routines
        self.by_kind = by_kind or []

    def read(self, q, **params):
        if "c.chunk_kind AS kind" in q:
            return self.by_kind
        if "AS source" in q:
            return []
        if ":Routine" in q:
            return [{"n": self.routines}]
        if ":Object" in q:
            return [{"n": self.objects}]
        raise AssertionError(f"unexpected query: {q}")


def test_tenant_layers_structural_only() -> None:
    layers = queries.tenant_layers(_LayersStore(objects=10, routines=5), "acme")
    assert layers["state"] == "structural_only"
    assert layers["vectorized"] is False and layers["callgraph_built"] is True
    assert layers["chunks"] == 0 and layers["graph_objects"] == 10


def test_tenant_layers_vectorized_with_code() -> None:
    st = _LayersStore(objects=10, routines=5,
                      by_kind=[{"kind": "object", "n": 7}, {"kind": "code", "n": 3}])
    layers = queries.tenant_layers(st, "acme")
    assert layers["state"] == "vectorized"
    assert layers["chunks"] == 10 and layers["code_chunks"] == 3
    assert layers["code_vectorized"] is True


def test_tenant_layers_metadata_vectors_without_code() -> None:
    st = _LayersStore(objects=10, routines=5, by_kind=[{"kind": "object", "n": 7}])
    layers = queries.tenant_layers(st, "acme")
    assert layers["state"] == "vectorized" and layers["code_vectorized"] is False


def test_tenant_layers_empty_and_docs_only() -> None:
    assert queries.tenant_layers(_LayersStore(), "ghost")["state"] == "empty"
    docs_only = _LayersStore(by_kind=[{"kind": "its", "n": 4}])
    assert queries.tenant_layers(docs_only, "docs")["state"] == "vectorized"


def test_tenant_layers_accepts_precomputed_totals() -> None:
    # With totals passed in, Object/Routine count queries must not run.
    class _ChunksOnly(_LayersStore):
        def read(self, q, **params):
            if ":Routine" in q or (":Object" in q and "count(o)" in q):
                raise AssertionError("totals were precomputed — must not be re-counted")
            return super().read(q, **params)

    layers = queries.tenant_layers(_ChunksOnly(), "acme", objects_total=3, routines_total=0)
    assert layers["state"] == "structural_only"
    assert layers["graph_objects"] == 3 and layers["callgraph_built"] is False


# --------------------------------------------------------------------------- #
# Профили публикации инструментов (onec-lite)
# --------------------------------------------------------------------------- #

def _lite_tool_names(monkeypatch, profile: str | None = None,
                     disabled: str | None = None) -> set[str]:
    """Имена опубликованных lite-тулов при заданном профиле (перезагружаем модуль сервера)."""
    import asyncio
    import importlib

    if profile is None:
        monkeypatch.delenv("ONEC_LITE_PROFILE", raising=False)
    else:
        monkeypatch.setenv("ONEC_LITE_PROFILE", profile)
    if disabled is None:
        monkeypatch.delenv("ONEC_LITE_DISABLED_TOOLS", raising=False)
    else:
        monkeypatch.setenv("ONEC_LITE_DISABLED_TOOLS", disabled)
    from onec_vecgraph.lite import server as lite_server

    mod = importlib.reload(lite_server)
    return {t.name for t in asyncio.run(mod.mcp.list_tools())}


def test_lite_tool_profiles(monkeypatch) -> None:
    """Схемы инструментов уходят клиенту на каждый запрос, поэтому состав — параметр.

    По умолчанию (lean) не публикуются тулы, дублирующие то, что у агента и так есть дешевле
    (rg / чтение файла / git): замерено, что list_routines против `rg '^(Процедура|Функция)'`
    стоит x0.28 по токенам, changed_objects против `git diff --name-status` — x0.20."""
    lean = _lite_tool_names(monkeypatch, None)
    full = _lite_tool_names(monkeypatch, "full")
    review = _lite_tool_names(monkeypatch, "review")

    assert len(review) < len(lean) < len(full) == 31
    # то, что нельзя выразить поиском по тексту, есть во всех профилях
    for core in ("find_callers", "find_overrides", "get_object", "review_set", "writes_to"):
        assert core in review and core in lean and core in full
    # bsl_sql — в lean: агрегаты через GROUP BY были нашим самым дорогим сценарием по токенам
    # (на живом УТ 462 токена против 37 437 у find_callers на том же вопросе)
    assert "bsl_sql" in lean and "bsl_sql" in full
    # дублирующее шелл — только в full
    for shellish in ("search_code", "changed_objects", "read_file", "list_routines"):
        assert shellish not in lean and shellish in full
    # точечное отключение работает поверх профиля
    trimmed = _lite_tool_names(monkeypatch, "full", "search_code find_routine")
    assert "search_code" not in trimmed and "find_routine" not in trimmed
    assert "find_callers" in trimmed
    _lite_tool_names(monkeypatch, None)  # вернуть модуль в дефолтное состояние


def test_instructions_do_not_advise_absent_tools(monkeypatch) -> None:
    """Инструкции сервера называют инструменты по имени — значит они обязаны быть в профиле.

    Расхождение «текст советует find_routine, а его в наборе нет» — это ровно тот сорт
    несогласованности, из-за которого ответам инструмента перестают верить: агент зовёт то, чего
    нет, получает ошибку и уходит в обход."""
    import importlib
    import re

    from onec_vecgraph.lite import server as lite_server

    # `review` — намеренно узкий набор, который оператор выбирает сам; инструкции для него
    # опираются на оговорку «список опубликованных инструментов авторитетен». Проверяем профили,
    # которые запускаются по умолчанию: именно там расхождение вводило бы агента в заблуждение.
    for profile in (None, "lean", "full"):
        names = _lite_tool_names(monkeypatch, profile)
        mod = importlib.reload(lite_server)
        text = mod.INSTRUCTIONS
        # Блок «ВМЕСТО КАКИХ ИНСТРУМЕНТОВ» упоминает тулы затем, чтобы их НЕ звать (там указана
        # замена через rg), поэтому их отсутствие в профиле — намеренное. Из проверки исключаем.
        for a, b in (("ВМЕСТО КАКИХ ИНСТРУМЕНТОВ", "Бери инструменты отсюда"),
                     ("Список опубликованных инструментов авторитетен", None)):
            start = text.find(a)
            end = text.find(b) if b else len(text)
            if 0 <= start < end:
                text = text[:start] + text[end:]
        advised = {
            m for m in re.findall(r"\b([a-z_]{4,})\b", text)
            if callable(getattr(mod, m, None)) and m not in {"main", "run", "configure"}
        }
        missing = sorted(a for a in advised if a not in names)
        assert not missing, f"профиль {profile or 'default'}: советуются, но отсутствуют {missing}"
    _lite_tool_names(monkeypatch, None)


def test_instructions_reference_existing_docs() -> None:
    """Файл, на который ссылаются инструкции сервера, обязан существовать в репозитории.

    `docs/bench_tools_vs_grep.md` был написан, но НЕ закоммичен: у меня локально ссылка
    открывалась, а после клона инструкции указывали в пустоту — агент шёл проверять числа и не
    находил их. Такая мелочь бьёт по доверию сильнее неточной цифры."""
    import re
    from pathlib import Path

    from onec_vecgraph.lite import server as lite_server

    import shutil
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    refs = sorted(set(
        re.findall(r"\b(docs/[\w./-]+\.md|scripts/[\w./-]+\.py)\b", lite_server.INSTRUCTIONS)))
    assert refs, "инструкции обязаны ссылаться на методику замеров"
    missing = [r for r in refs if not (repo / r).exists()]
    assert not missing, f"инструкции ссылаются на отсутствующие файлы: {missing}"
    if shutil.which("git") is None or not (repo / ".git").exists():
        pytest.skip("git недоступен — проверка отслеживания невозможна")
    # Наличия на диске НЕ достаточно: у автора файл есть, а после клона ссылка ведёт в пустоту.
    untracked = [r for r in refs if subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", r],
        capture_output=True).returncode != 0]
    assert not untracked, f"инструкции ссылаются на неотслеживаемые git файлы: {untracked}"


def test_doc_anchor_links_resolve() -> None:
    """Ссылки на разделы внутри документации не должны висеть в воздухе.

    Переименование раздела осиротило ссылку `#какой-поиск-выбрать` в том же файле: текст читался
    как рабочий, а клик никуда не вёл. Проверяем в лоб — генерируем якоря из заголовков по
    правилу GitHub (lower, без пунктуации, пробелы -> дефисы)."""
    import re
    from pathlib import Path

    def anchor(heading: str) -> str:
        text = re.sub(r"[*`\[\]()]", "", heading.strip().lower())
        return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", text).strip())

    repo = Path(__file__).resolve().parent.parent
    docs = ["README.md", "AGENTS.md", "PLAN.md"]
    docs += [p.relative_to(repo).as_posix() for p in sorted((repo / "docs").glob("*.md"))]
    texts = {d: (repo / d).read_text(encoding="utf-8") for d in docs if (repo / d).exists()}
    anchors = {
        d: {anchor(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)", t, re.M)}
        for d, t in texts.items()
    }
    broken: list[str] = []
    for doc, text in texts.items():
        base = (repo / doc).parent
        for target, frag in re.findall(r"\]\(([^)#\s]*)#([^)\s]+)\)", text):
            if target.startswith(("http://", "https://")):
                continue
            rel = (base / target).resolve() if target else (repo / doc)
            if not rel.is_relative_to(repo):
                continue
            key = rel.relative_to(repo).as_posix()
            if key not in anchors:  # цель вне набора проверяемых файлов
                continue
            if frag not in anchors[key]:
                broken.append(f"{doc} -> {key}#{frag}")
    assert not broken, "битые ссылки на разделы: " + "; ".join(broken)


def test_index_state_is_reachable_from_published_tool(monkeypatch) -> None:
    """Признак деградации индекса обязан быть в инструменте, который ПУБЛИКУЕТСЯ по умолчанию.

    Состояние индекса я сначала добавил только в metrics — а его в профиле `lean` нет, так что
    агент о медленном скане с null-счётчиками не узнал бы ничего. Наблюдаемость, доступная лишь
    оператору через админку, не считается: решение о доверии к ответу принимает агент."""
    names = _lite_tool_names(monkeypatch, None)
    carriers = {"overview", "metrics"}
    published = carriers & names
    assert published, f"ни один носитель состояния индекса не опубликован: {sorted(carriers)}"
    assert "overview" in published, "overview обязан оставаться в профиле по умолчанию"

    import inspect

    from onec_vecgraph.lite import server as lite_server

    doc = inspect.getdoc(lite_server.overview) or ""
    assert "index" in doc, "overview обязан описывать блок index в своём описании"
    src = inspect.getsource(lite_server.overview)
    assert "_index_brief" in src, "overview обязан отдавать состояние индекса"


def test_every_published_lite_tool_is_documented(monkeypatch) -> None:
    """Новый инструмент обязан попасть в руководство, а не только в код.

    bsl_sql я добавил в инструкции сервера и в таблицу маршрутизации, но забыл про инвентарь
    инструментов в LITE_USAGE и про README с AGENTS.md — агент, который читает документацию, а не
    схемы, о нём бы не узнал. Проверяем весь профиль full, чтобы расхождение ловилось сразу."""
    from pathlib import Path

    names = _lite_tool_names(monkeypatch, "full")
    doc = (Path(__file__).resolve().parent.parent / "docs" / "LITE_USAGE.md").read_text(
        encoding="utf-8")
    missing = sorted(n for n in names if f"`{n}`" not in doc)
    assert not missing, f"инструменты не описаны в docs/LITE_USAGE.md: {missing}"
    _lite_tool_names(monkeypatch, None)  # вернуть модуль в дефолтное состояние
