"""Корпус `wiki`: раздел Яндекс Вики рекурсивно, с инкрементом по дате правки страницы.

ЕДИНИЦА ВЕРСИОНИРОВАНИЯ — СТРАНИЦА, а не секция внутри неё. Причина не в удобстве: версия
(``modified_at``) существует только на уровне страницы, и узнать её можно, не загружая
содержимое. Если бы единицей была секция, состав секций был бы неизвестен до загрузки, и
дешёвая проверка «что изменилось» стала бы невозможной — каждый холостой прогон качал бы весь
раздел целиком (для целевого раздела это 1076 страниц и ~15 МБ текста).

Плата за решение: разбиение на чанки идёт по размеру (``chunking.doc_chunks``), а не по
заголовкам, поэтому хлебные крошки чанка — это путь СТРАНИЦЫ в дереве Вики, а не путь
заголовка внутри неё. Заголовки при этом остаются в тексте чанка как есть (разметка не
вычищается), так что для поиска они не теряются.

``external_id`` — числовой ID страницы, а НЕ slug. Страницы в Вики переименовывают и переносят;
при slug-ключе каждый перенос выглядел бы как «одну удалили, другую добавили»: лишний
эмбеддинг и разорванная история. ID переносы переживает.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import DocUnit, Source, sha1_text
from .markdown import split_markdown_sections
from .wiki_client import WikiClient, WikiError, normalize_slug

__all__ = ["WikiSource", "effective_access", "UNIT_SCHEMA", "ACCESS_MODES",
           "ACCESS_PUBLIC", "ACCESS_RESTRICTED", "ACCESS_ALL"]

# Метка формата единицы. Входит в version_hash, поэтому изменение сборки текста/заголовка
# заставит переиндексировать корпус, а не оставит в базе смесь старого и нового формата.
UNIT_SCHEMA = "wiki-v1"

# Уровни доступа, при которых страница считается ОБЩЕДОСТУПНОЙ внутри организации.
PUBLIC_ACCESS_LEVELS = ("all_staff",)

# Режим отбора страниц по доступу. Три значения покрывают раздел БЕЗ ЩЕЛЕЙ: public ∪ restricted
# = all, поэтому страница с незнакомым уровнем доступа не пропадает из обоих корпусов молча —
# она гарантированно попадёт в restricted.
ACCESS_PUBLIC = "public"          # только общедоступные — корпус для ОБЩЕГО тенанта
ACCESS_RESTRICTED = "restricted"  # только закрытые — корпус для тенанта КОНКРЕТНОГО проекта
ACCESS_ALL = "all"
ACCESS_MODES = (ACCESS_PUBLIC, ACCESS_RESTRICTED, ACCESS_ALL)
DEFAULT_ACCESS_MODE = ACCESS_PUBLIC

PAGE_URL_TEMPLATE = "https://wiki.yandex.ru/{slug}/"

# Бюджет слияния коротких разделов — тот же, что у чанкера (chunking._CODE_BUDGET_NONWS).
# Держим числом здесь, а не импортом: sources не должны зависеть от внутренностей chunking,
# а расхождение безопасно — слитый раздел сверх бюджета чанкер просто дорежет.
SECTION_MERGE_BUDGET = 1200


def _nonws(text: str) -> int:
    return len("".join(text.split()))


def _common_prefix(left: list[str], right: list[str]) -> list[str]:
    out: list[str] = []
    for a, b in zip(left, right):
        if a != b:
            break
        out.append(a)
    return out


def _demote_headings(section: dict, common: list[str]) -> str:
    """Заголовки, уходящие из пути при слиянии, возвращаются в ТЕКСТ раздела.

    Без этого слияние их теряло совсем: в пути они схлопывались до общего предка, а в теле их
    никогда и не было — `split_markdown_sections` выносит заголовок из body в title."""
    dropped = section["path"][len(common):]
    return "\n".join([*dropped, section["body"]]) if dropped else section["body"]


def _page_sections(text: str, budget: int = SECTION_MERGE_BUDGET) -> list[dict]:
    """Разделы страницы по заголовкам → ``[{path: [заголовки], body: текст}]``.

    ДВА ШАГА, и второй не менее важен первого.

    1. Разрезать по заголовкам — чтобы чанк нёс свой подраздел, а не только положение
       страницы в дереве Вики.
    2. **Слить подряд идущие короткие разделы** до бюджета. Замер на живом разделе: без
       слияния медиана чанка ~260 непробельных символов, то есть страница распадается на
       заголовок-плюс-пара-строк. Такие огрызки эмбеддятся плохо — сигнала мало, а в выдаче
       они занимают место наравне с содержательными кусками. Это вторая половина подхода
       cAST (split-then-merge), без неё разбиение по заголовкам ухудшает поиск, а не улучшает.

    При слиянии заголовок вливаемого раздела возвращается в текст строкой, а путь схлопывается
    до ОБЩЕГО предка — иначе объединённый чанк выдавал бы себя за один конкретный подраздел.

    Пустой список, если делить нечего: тогда движок режет страницу по размеру, как раньше.
    """
    raw: list[dict] = []
    for section in split_markdown_sections(text):
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        title = str(section.get("title") or "").strip()
        path = [*(section.get("path") or []), title] if title else list(section.get("path") or [])
        raw.append({"path": [p for p in path if p], "body": body})

    merged: list[dict] = []
    for section in raw:
        previous = merged[-1] if merged else None
        if previous is not None and _nonws(previous["body"]) + _nonws(section["body"]) <= budget:
            common = _common_prefix(previous["path"], section["path"])
            previous["body"] = (
                f"{_demote_headings(previous, common)}\n\n{_demote_headings(section, common)}")
            previous["path"] = common
            continue
        merged.append(dict(section))
    return merged if len(merged) > 1 else []


def effective_access(access_policy: Any) -> str:
    """Действующий уровень доступа страницы: собственный либо унаследованный."""
    if not isinstance(access_policy, dict):
        return ""
    access_type = str(access_policy.get("access_type") or "")
    if access_type == "inherited":
        return str(access_policy.get("inherited_access_type") or "")
    return access_type


class WikiSource(Source):
    """Запись манифеста::

        - type: wiki
          root: homepage/gt-products/1s      # slug или полный URL раздела
          access_mode: public                # public | restricted | all (по умолчанию public)
          doc_topic: docs                    # фасет классификации, опц.
          corpus_version: wiki:2026-07-30    # опц.
          public_access_levels: [all_staff]  # опц., что считать общедоступным
          include_root: true                 # индексировать ли саму страницу раздела

    ``access_mode`` — главный переключатель безопасности. ``public`` собирает только
    общедоступные страницы и предназначен для ОБЩЕГО тенанта; ``restricted`` собирает
    остальные и предназначен для тенанта КОНКРЕТНОГО проекта, где их увидят лишь те, у кого
    есть доступ к проекту. Вместе они покрывают раздел целиком, поэтому страница с незнакомым
    уровнем доступа не теряется — она уходит в закрытую часть, а не в общую.
    """

    name = "wiki"
    source = "wiki"
    owner_label = "Document"

    def __init__(self, entry: dict, client: WikiClient | None = None) -> None:
        self.entry = entry
        self.root = normalize_slug(entry.get("root") or entry.get("slug") or "")
        if not self.root:
            raise ValueError("запись корпуса wiki требует 'root' — slug или URL раздела")
        self.doc_topic = entry.get("doc_topic") or "docs"
        self.corpus_version = entry.get("corpus_version")
        self.include_root = bool(entry.get("include_root", True))
        self.access_mode = str(entry.get("access_mode") or DEFAULT_ACCESS_MODE)
        if self.access_mode not in ACCESS_MODES:
            raise ValueError(
                f"access_mode должен быть одним из {ACCESS_MODES}, получено {self.access_mode!r}")
        levels = entry.get("public_access_levels")
        self.public_levels = tuple(levels) if levels else PUBLIC_ACCESS_LEVELS
        # Шов для офлайн-тестов: клиент подставляется снаружи, иначе строится из окружения.
        self._client = client
        self._only: set[str] | None = None
        self._catalog: dict[str, dict[str, Any]] | None = None
        self.skipped_by_access: list[str] = []
        self.unreadable: list[str] = []
        self.vanished: list[str] = []

    # --- служебное -------------------------------------------------------------------

    def _accepts(self, level: str) -> bool:
        """Берём ли страницу с таким уровнем доступа.

        Общедоступная часть уезжает в ОБЩИЙ тенант, видимый всем проектам; закрытая — в тенант
        конкретного проекта, где её увидят только те, у кого есть доступ к этому проекту.
        Смешивать их в одном корпусе нельзя: поимённые списки доступа Вики иначе обходятся
        семантическим поиском.
        """
        if self.access_mode == ACCESS_ALL:
            return True
        is_public = level in self.public_levels
        return is_public if self.access_mode == ACCESS_PUBLIC else not is_public

    @property
    def client(self) -> WikiClient:
        if self._client is None:
            self._client = WikiClient.from_env()
        return self._client

    def _load_catalog(self) -> dict[str, dict[str, Any]]:
        """{external_id: {slug, title, modified_at}} по всему поддереву раздела.

        Один обход листинга (курсорная пагинация до конца) плюс параллельная выборка
        ``fields=attributes,access_policy`` — без содержимого. Это и есть дешёвая проверка
        версий: ответ на страницу ~300 байт вместо ~14 КБ.
        """
        if self._catalog is not None:
            return self._catalog

        root_page = self.client.page(self.root)
        root_id = root_page.get("id")
        if not root_id:
            raise WikiError(f"раздел не найден: {self.root}")

        entries: list[dict[str, Any]] = []
        if self.include_root:
            entries.append({"id": root_id, "slug": root_page.get("slug") or self.root})
        entries.extend(self.client.descendants(root_id))

        by_slug = {}
        for item in entries:
            slug = str(item.get("slug") or "").strip("/")
            if slug:
                by_slug[slug] = item

        catalog: dict[str, dict[str, Any]] = {}
        self.skipped_by_access = []
        self.unreadable = []
        self.vanished = []
        for slug, data, error in self.client.pages_bulk(
            sorted(by_slug), ("attributes", "access_policy")
        ):
            if error is not None and error.is_missing:
                # Страницы правда нет — она удалена из Вики между листингом и выборкой.
                # Это единственный случай, когда пропуск законен.
                self.vanished.append(slug)
                continue
            if error is not None or not data:
                self.unreadable.append(slug)
                continue
            if not self._accepts(effective_access(data.get("access_policy"))):
                self.skipped_by_access.append(slug)
                continue
            attributes = data.get("attributes") or {}
            external_id = str(data.get("id") or by_slug[slug].get("id") or "")
            if not external_id:
                self.unreadable.append(slug)
                continue
            catalog[external_id] = {
                "slug": str(data.get("slug") or slug),
                "title": str(data.get("title") or slug),
                "modified_at": str(attributes.get("modified_at") or ""),
            }
        if self.unreadable:
            # НЕ отдаём неполный каталог. Страница, выпавшая из versions(), для движка
            # неотличима от удалённой — он сотрёт её вместе с чанками. Лучше оборвать прогон
            # и оставить корпус прежним, чем тихо выкосить раздел из-за троттлинга API.
            raise WikiError(
                f"обход раздела {self.root} неполон: не прочитано {len(self.unreadable)} "
                f"страниц из {len(by_slug)} (например {', '.join(self.unreadable[:3])}). "
                "Индексация прервана, чтобы недостающие страницы не были удалены из корпуса."
            )
        self._catalog = catalog
        return catalog

    # --- контракт Source -------------------------------------------------------------

    def versions(self) -> dict[str, str]:
        """Версии всех страниц раздела БЕЗ загрузки содержимого (см. Source.versions)."""
        return {
            external_id: sha1_text(UNIT_SCHEMA, meta["modified_at"] or meta["slug"])
            for external_id, meta in self._load_catalog().items()
        }

    def restrict_to(self, external_ids: set[str]) -> None:
        self._only = set(external_ids)

    def units(self) -> Iterator[DocUnit]:
        catalog = self._load_catalog()
        wanted = sorted(catalog) if self._only is None else sorted(self._only & set(catalog))
        if not wanted:
            return
        slug_by_id = {external_id: catalog[external_id]["slug"] for external_id in wanted}
        id_by_slug = {slug: external_id for external_id, slug in slug_by_id.items()}

        for slug, data, error in self.client.pages_bulk(sorted(id_by_slug), ("content", "breadcrumbs")):
            if error is not None and error.is_missing:
                self.vanished.append(slug)
                continue
            if error is not None:
                # Здесь пропуск тоже опасен, но иначе: единица не попадёт в fetched, движок
                # подставит заглушку со СТАРОЙ версией — и страница останется в корпусе в
                # прежней редакции, хотя она изменилась. Молчать об этом нельзя.
                raise WikiError(
                    f"не удалось загрузить изменившуюся страницу {slug}: {error}. "
                    "Индексация прервана, чтобы в корпусе не осталась устаревшая редакция."
                ) from error
            if not data:
                self.unreadable.append(slug)
                continue
            external_id = id_by_slug[slug]
            meta = catalog[external_id]
            text = str(data.get("content") or "").strip()
            if not text:
                continue
            title = str(data.get("title") or meta["title"])
            # Хлебные крошки из API — настоящие заголовки разделов, а не сегменты slug:
            # «🏛 GrandTrade Вики ▸ 🏷️ Продукты GT ▸ 1С ▸ 1С:УТ». Последний элемент — сама
            # страница, её заголовок и так идёт отдельным полем.
            crumbs = [str(c.get("title") or "").strip()
                      for c in (data.get("breadcrumbs") or []) if isinstance(c, dict)]
            crumbs = [c for c in crumbs[:-1] if c] if crumbs else []
            yield DocUnit(
                external_id=external_id,
                title=title,
                text=text,
                version_hash=sha1_text(UNIT_SCHEMA, meta["modified_at"] or slug),
                section_path=crumbs,
                # Разбиение по заголовкам страницы: чанк из середины длинного описания релизов
                # несёт в крошках свой подраздел, а не только положение страницы в дереве.
                # Версионируется при этом по-прежнему страница целиком — иначе инкремента нет.
                sections=_page_sections(text),
                source_url=PAGE_URL_TEMPLATE.format(slug=meta["slug"]),
                extra={
                    "doc_topic": self.doc_topic,
                    "corpus_version": self.corpus_version,
                    "wiki_slug": meta["slug"],
                    "modified_at": meta["modified_at"] or None,
                },
            )

    def report(self) -> dict[str, Any]:
        """Что осталось за бортом обхода — для лога прогона, а не для тишины."""
        return {
            "root": self.root,
            "pages_total": len(self._catalog or {}),
            "skipped_by_access": len(self.skipped_by_access),
            "vanished": len(self.vanished),
            "unreadable": len(self.unreadable),
        }
