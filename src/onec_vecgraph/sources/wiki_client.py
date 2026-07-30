"""HTTP-клиент API Яндекс Вики для адаптера корпуса `wiki`.

Отдельно от MCP `yandex-wiki` намеренно: тот обслуживает агентов (срезы по limit, обрезка
содержимого по max_chars, дружелюбные ошибки), а выгрузке нужен полный обход с пагинацией.
Через MCP он был бы ещё и лишним звеном, которое может лежать ровно тогда, когда идёт индексация.

КОНТРАКТ API (проверено на живой организации 2026-07-30):

* ``GET /pages?slug=<slug>&fields=<список>`` — страница. Без ``fields`` отдаёт только
  ``id, slug, title, page_type``. Допустимые поля: ``redirect, breadcrumbs, attributes,
  content, access_policy, access_lists, owner``.
* ``GET /pages/{id}/descendants`` — ВСЁ поддерево плоским списком, **курсорной пагинацией**
  (``results``, ``next_cursor``). В элементе только ``id`` и ``slug``.
* Заголовки: ``Authorization: OAuth <token>`` и заголовок организации
  (``X-Org-Id`` для Яндекс 360, ``X-Cloud-Org-Id`` для Identity Hub).

ДВЕ ОСОБЕННОСТИ, НА КОТОРЫХ ЛЕГКО ОБЖЕЧЬСЯ:

1. **Версия страницы есть только в ``attributes``** (``modified_at``), и её НЕТ в листинге
   потомков. Поэтому дешёвая проверка «что изменилось» — это отдельный обход всех страниц с
   ``fields=attributes``: ответ ~300 байт против ~14 КБ с содержимым, то есть впятидесятеро
   меньше трафика при том же числе запросов.
2. **ETag и Last-Modified отсутствуют** — условные запросы (``If-None-Match``) невозможны,
   дешевле обхода ничего нет.
3. **API ЖЁСТКО ТРОТТЛИТ.** Замерено на живом разделе: потолок ~8 запросов/с независимо от
   параллелизма, а лишние запросы просто отбрасываются (60 страниц: workers=1 → 60 успешных,
   workers=4 → 26, workers=8 → 15). Поэтому параллелизм по умолчанию низкий, а повторы
   обязательны — без них «не прочиталось» становится тихой потерей страниц.

РАЗЛИЧАТЬ 404 И ВРЕМЕННЫЙ СБОЙ ОБЯЗАТЕЛЬНО. Страница, выпавшая из обхода, для движка выглядит
удалённой — он сотрёт её из корпуса. Значит 404 (страницы правда больше нет) и 429/5xx/сеть
(она есть, но не ответила) — разные вещи: первое отдаётся как отсутствие, второе поднимается
ошибкой и обрывает прогон. Лучше не обновиться вовсе, чем молча выкосить раздел.

TLS: в корпоративной сети внешний трафик переподписан внутренним CA, который есть в системном
хранилище, но отсутствует в бандле certifi. Поэтому по умолчанию берётся системное хранилище,
как это делает MCP; путь к своему бандлу — ``WIKI_CA_BUNDLE``.
"""

from __future__ import annotations

import os
import random
import ssl
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import unquote, urlsplit

DEFAULT_API_BASE = "https://api.wiki.yandex.net/v1"
DEFAULT_ORG_HEADER = "X-Org-Id"
DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 100
# Замерено: потолок API ~8 запросов/с, дальше он отбрасывает лишнее. Двух потоков достаточно,
# чтобы этот потолок выбрать; больше — только рост доли отброшенных и повторов.
DEFAULT_WORKERS = 2
DEFAULT_RETRIES = 4
DEFAULT_RETRY_DELAY = 1.0
# Коды, при которых имеет смысл повторить: троттлинг и временные сбои сервера.
RETRIABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class WikiError(RuntimeError):
    """Ошибка обращения к API Вики.

    ``status`` несёт HTTP-код, когда он был: 404 отличается от прочих принципиально — это
    «страницы нет», а не «не смогли прочитать», и только оно даёт право удалить её из корпуса.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_missing(self) -> bool:
        return self.status == 404


def normalize_slug(slug_or_url: str) -> str:
    """«https://wiki.yandex.ru/homepage/gt-products/1s/» → «homepage/gt-products/1s»."""
    raw = str(slug_or_url or "").strip()
    if "://" in raw:
        raw = urlsplit(raw).path
    return unquote(raw).strip("/")


class WikiClient:
    """Тонкий синхронный клиент. Создаётся из окружения или с явными параметрами (для тестов)."""

    def __init__(
        self,
        token: str,
        org_id: str,
        *,
        base_url: str = DEFAULT_API_BASE,
        org_header: str = DEFAULT_ORG_HEADER,
        timeout: int = DEFAULT_TIMEOUT,
        ca_bundle: str | None = None,
        workers: int = DEFAULT_WORKERS,
        retries: int = DEFAULT_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ) -> None:
        if not token or not org_id:
            raise WikiError("не заданы токен и/или идентификатор организации Вики")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.workers = max(1, int(workers))
        self.retries = max(0, int(retries))
        self.retry_delay = float(retry_delay)
        self._headers = {"Authorization": f"OAuth {token}", org_header: org_id}
        self._ca_bundle = ca_bundle

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **overrides: Any) -> WikiClient:
        """Имена переменных совместимы с MCP yandex-wiki и yandex-tracker — один .env на всех."""
        env = dict(env if env is not None else os.environ)
        return cls(
            env.get("WIKI_TOKEN") or env.get("TRACKER_TOKEN") or "",
            env.get("WIKI_ORG_ID") or env.get("TRACKER_ORG_ID") or "",
            base_url=env.get("WIKI_API_BASE") or DEFAULT_API_BASE,
            org_header=env.get("WIKI_ORG_HEADER") or DEFAULT_ORG_HEADER,
            ca_bundle=env.get("WIKI_CA_BUNDLE") or None,
            **overrides,
        )

    def _client(self):
        import httpx  # ленивый импорт: обязателен только для корпуса wiki (extra «wiki»)

        return httpx.Client(
            timeout=self.timeout,
            verify=ssl.create_default_context(cafile=self._ca_bundle),
            headers=self._headers,
        )

    def _get(self, client: Any, path: str, params: dict[str, Any] | None = None) -> Any:
        """Запрос с повторами. 404 не повторяется — страницы просто нет."""
        import httpx

        delay = self.retry_delay
        last: WikiError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = client.get(f"{self.base_url}{path}", params=params or {})
                if response.status_code in RETRIABLE_STATUSES:
                    last = WikiError(f"{path}: HTTP {response.status_code}", response.status_code)
                    if attempt < self.retries:
                        # Retry-After уважаем, если сервер его прислал; иначе растущая пауза
                        # с разбросом, чтобы потоки не ломились в API одновременно.
                        try:
                            suggested = float(response.headers.get("Retry-After") or 0)
                        except ValueError:
                            suggested = 0.0
                        time.sleep(max(suggested, delay) + random.uniform(0, 0.3))
                        delay *= 2
                        continue
                    raise last
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as error:
                raise WikiError(
                    f"{path}: HTTP {error.response.status_code} {error.response.text[:200]}",
                    error.response.status_code,
                ) from error
            except httpx.HTTPError as error:
                last = WikiError(f"{path}: {error}")
                if attempt < self.retries:
                    time.sleep(delay + random.uniform(0, 0.3))
                    delay *= 2
                    continue
                raise last from error
        raise last or WikiError(f"{path}: не удалось выполнить запрос")

    def page(self, slug: str, fields: tuple[str, ...] = ()) -> dict[str, Any]:
        with self._client() as client:
            params = {"slug": slug}
            if fields:
                params["fields"] = ",".join(fields)
            data = self._get(client, "/pages", params)
        return data if isinstance(data, dict) else {}

    def descendants(self, page_id: int | str, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict[str, Any]]:
        """ВСЕ потомки раздела. Пагинация обходится до конца — оборвать её на первой порции
        значит тихо проиндексировать кусок раздела и отчитаться как за целое."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        with self._client() as client:
            while True:
                params: dict[str, Any] = {"page_size": int(page_size)}
                if cursor:
                    params["cursor"] = cursor
                data = self._get(client, f"/pages/{page_id}/descendants", params)
                batch = (data.get("results") if isinstance(data, dict) else data) or []
                out.extend(item for item in batch if isinstance(item, dict))
                cursor = data.get("next_cursor") if isinstance(data, dict) else None
                if not cursor or not batch:
                    break
        return out

    def pages_bulk(
        self, slugs: list[str], fields: tuple[str, ...]
    ) -> Iterator[tuple[str, dict[str, Any] | None, WikiError | None]]:
        """Параллельная выборка страниц → ``(slug, данные, ошибка)``.

        Ошибка отдаётся ВЫЗЫВАЮЩЕМУ, а не проглатывается: 404 значит «страницы нет» и разрешает
        удалить её из корпуса, всё остальное — «не смогли прочитать», и молча пропустить такую
        страницу нельзя, иначе движок сочтёт её удалённой. Клиент здесь один на весь обход —
        переиспользование соединений заметно при тысяче запросов.
        """
        if not slugs:
            return
        params_fields = ",".join(fields) if fields else None
        with self._client() as client:

            def fetch(slug: str) -> tuple[str, dict[str, Any] | None, WikiError | None]:
                params: dict[str, Any] = {"slug": slug}
                if params_fields:
                    params["fields"] = params_fields
                try:
                    data = self._get(client, "/pages", params)
                except WikiError as error:
                    return slug, None, error
                return slug, (data if isinstance(data, dict) else None), None

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                yield from pool.map(fetch, slugs)
