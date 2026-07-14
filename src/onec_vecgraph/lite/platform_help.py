"""Platform syntax-assistant help (.hbk) for the lite server — direct reading, no Neo4j.

Deliberately thin over the big pipeline's parsing so the two implementations cannot drift:
path resolution/validation is `sources.hbk.HbkSource.validate()` (bin dir | .hbk file,
version auto-detected from the path), container reading is `sources.hbk_container`, page
parsing is `sources.hbk._parse_page`. The only lite-specific part is WHERE the data lives:
an in-memory name index (built lazily, ~seconds) instead of vectorized `:Document` nodes,
with page text re-read from the container on demand. Lookup semantics of `docinfo` mirror
`queries.docinfo` (RU / EN / «Объект.Метод», optional version, disambiguation list).

Topic counts may differ slightly from the big server: the index keeps every page with a
title, while ingest also drops pages with empty text."""

from __future__ import annotations

import io
import re
import zipfile
from html import unescape

from ..sources import hbk_container
from ..sources.hbk import HbkSource, _NAME, _parse_page  # noqa: PLC2701 - shared parsing core

_H1 = re.compile(rb"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def parse_help_lines(text: str) -> list[dict]:
    """Admin/CLI form: one entry per line — `путь` или `версия = путь` (';' тоже разделитель)."""
    out: list[dict] = []
    for line in (text or "").replace(";", "\n").splitlines():
        line = line.strip().strip('"').strip()
        if not line:
            continue
        version, sep, path = line.partition("=")
        if sep and path.strip():
            out.append({"version": version.strip(), "path": path.strip().strip('"')})
        else:
            out.append({"version": "", "path": line})
    return out


def render_help_lines(entries: list[dict]) -> str:
    return "\n".join(
        (f"{e.get('version')} = {e.get('path')}" if e.get("version") else str(e.get("path", "")))
        for e in entries
    )


def _resolve_files(entry: dict) -> list[tuple[str, str, str]]:
    """(hbk_path, platform_version, help_kind) rows for one config entry, via HbkSource."""
    path = str(entry.get("path") or "")
    spec: dict = {"platform_version": entry.get("version") or None}
    if path.lower().endswith(".hbk"):
        spec["files"] = [path]
    else:
        spec["bin"] = path
    return HbkSource(spec).validate()


def _pv_key(pv: str) -> tuple:
    """Numeric-aware sort key: '8.3.27.2130' > '8.3.9.100'; non-numeric builds sort last."""
    nums = [int(x) for x in re.findall(r"\d+", pv or "")]
    return (0, nums) if nums else (1, [])


def _fast_title(html: bytes) -> str | None:
    """<h1> text without a full lxml parse (the index only needs names); None -> use _parse_page."""
    m = _H1.search(html)
    if not m:
        return None
    raw = _TAG.sub("", m.group(1).decode("utf-8", "replace"))
    return unescape(raw).strip() or None


def _split_name(title: str) -> tuple[str, str | None]:
    m = _NAME.match(title)
    return (m.group("ru").strip(), m.group("en").strip()) if m else (title, None)


class HelpCatalog:
    """Configured .hbk files + a lazily built in-memory name index."""

    def __init__(self) -> None:
        self.entries: list[dict] = []  # as configured: {"version": str, "path": str}
        self._files: list[tuple[str, str, str]] = []  # resolved (hbk_path, pv, help_kind)
        self._index: list[dict] | None = None  # topic rows (no text)
        self._zips: dict[str, zipfile.ZipFile] = {}

    # ------------------------------------------------------------- configuration

    def configure(self, entries: list[dict]) -> list[str]:
        """Validate + swap the config; returns per-entry error messages (empty = all ok).

        Valid entries are kept even when some fail, so one typo doesn't drop the rest."""
        errors: list[str] = []
        files: list[tuple[str, str, str]] = []
        kept: list[dict] = []
        for e in entries:
            try:
                resolved = _resolve_files(e)
            except (ValueError, FileNotFoundError) as exc:
                errors.append(f"{e.get('path')}: {exc}")
                continue
            kept.append({"version": e.get("version") or "", "path": str(e.get("path") or ""),
                         **({"limit": e["limit"]} if e.get("limit") else {})})
            files.extend(resolved)
        self.entries = kept
        self._files = files
        self._index = None
        self._close_zips()
        return errors

    def refresh(self) -> None:
        self._index = None
        self._close_zips()

    def _close_zips(self) -> None:
        for z in self._zips.values():
            try:
                z.close()
            except Exception:  # noqa: BLE001
                pass
        self._zips.clear()

    # ------------------------------------------------------------- index

    def _limit_for(self, hbk_path: str) -> int | None:
        for e in self.entries:
            if e.get("limit") and str(e.get("path", "")) in hbk_path:
                return int(e["limit"])
        return None

    def index(self) -> list[dict]:
        """Build (once) the topic index: title/en/name norms + page address, no text."""
        if self._index is not None:
            return self._index
        rows: list[dict] = []
        for hbk_path, pv, help_kind in self._files:
            limit = self._limit_for(hbk_path)
            n = 0
            for zip_path, html in hbk_container.iter_html_pages(hbk_path):
                title = _fast_title(html)
                if title is None:
                    try:
                        title = _parse_page(html)[0]
                    except Exception:  # noqa: BLE001 - malformed page
                        continue
                if not title:
                    continue
                ru, en = _split_name(title)
                rows.append({
                    "hbk": hbk_path,
                    "page": zip_path,
                    "platform_version": pv,
                    "help_kind": help_kind,
                    "title": ru,
                    "en_name": en,
                    "full_name_norm": ru.lower(),
                    "name_norm": (ru.split(".")[-1] if ru else "").lower(),
                })
                n += 1
                if limit and n >= limit:
                    break
        self._index = rows
        return rows

    def indexed(self) -> bool:
        return self._index is not None

    # ------------------------------------------------------------- reading pages

    def _zip(self, hbk_path: str) -> zipfile.ZipFile | None:
        z = self._zips.get(hbk_path)
        if z is not None:
            return z
        fs = hbk_container.named_elements(hbk_path).get("FileStorage")
        if not fs or fs[:4] != b"PK\x03\x04":
            return None
        z = zipfile.ZipFile(io.BytesIO(fs))
        self._zips[hbk_path] = z
        return z

    def _doc(self, row: dict) -> dict:
        z = self._zip(row["hbk"])
        text = ""
        if z is not None:
            try:
                _ru, _en, text = _parse_page(z.read(row["page"]))
            except Exception:  # noqa: BLE001 - page gone/broken: return meta anyway
                text = ""
        return {
            "found": True,
            "fqn": f"platform_help:{row['platform_version']}|{row['title']}",
            "title": row["title"],
            "en_name": row["en_name"],
            "platform_version": row["platform_version"],
            "help_kind": row["help_kind"],
            "source": "platform_help",
            "text": text,
        }

    # ------------------------------------------------------------- queries

    def versions(self) -> dict:
        """Configured builds with file/topic counts (topics None until the index is built)."""
        agg: dict[str, dict] = {}
        for _p, pv, hk in self._files:
            e = agg.setdefault(pv, {"platform_version": pv, "files": 0, "by_help_kind": {}, "topics": None})
            e["files"] += 1
            e["by_help_kind"].setdefault(hk, None)
        if self._index is not None:
            for pv in agg:
                agg[pv]["topics"] = 0
                agg[pv]["by_help_kind"] = {}
            for row in self._index:
                e = agg.get(row["platform_version"])
                if e is None:
                    continue
                e["topics"] += 1
                hk = row["help_kind"]
                e["by_help_kind"][hk] = e["by_help_kind"].get(hk, 0) + 1
        def _newest(v: dict) -> tuple:
            kind, nums = _pv_key(v["platform_version"])
            return (kind, [-x for x in nums])

        versions = sorted(agg.values(), key=_newest)
        return {"versions": versions, "count": len(versions), "indexed": self._index is not None}

    def docinfo(self, name: str, platform_version: str = "") -> dict:
        """Exact lookup by canonical name (RU / EN / «Объект.Метод»), mirrors queries.docinfo."""
        if not self._files:
            return {"found": False, "error": "Справка платформы не настроена (пути в админке /admin)."}
        n = name.strip().lower()
        pv = platform_version.strip()
        cands = [
            r for r in self.index()
            if (not pv or r["platform_version"] == pv)
            and (r["full_name_norm"] == n or r["name_norm"] == n
                 or (r["en_name"] or "").lower() == n)
        ]
        if not cands:
            return {"found": False, "name": name, "platform_version": platform_version or None}
        def _rank(r: dict) -> tuple:
            kind, nums = _pv_key(r["platform_version"])
            return (0 if r["full_name_norm"] == n else 1, kind, [-x for x in nums])

        cands.sort(key=_rank)
        distinct = {(r["full_name_norm"], r["platform_version"]) for r in cands}
        if len(distinct) == 1:
            return self._doc(cands[0])
        return {
            "found": True, "name": name, "ambiguous": True,
            "candidates": [
                {"fqn": f"platform_help:{r['platform_version']}|{r['title']}", "title": r["title"],
                 "platform_version": r["platform_version"], "help_kind": r["help_kind"]}
                for r in cands[:25]
            ],
        }

    def get_document(self, name: str, platform_version: str = "") -> dict:
        """Full topic text by exact full name; accepts the big server's fqn form
        `platform_help:<версия>|<Имя>` as well as plain «Объект.Метод» + version arg."""
        if not self._files:
            return {"found": False, "error": "Справка платформы не настроена (пути в админке /admin)."}
        q = name.strip()
        pv = platform_version.strip()
        if q.lower().startswith("platform_help:"):
            q = q.partition(":")[2]
        ver, sep, rest = q.partition("|")
        if sep and rest:
            pv, q = ver.strip(), rest.strip()
        low = q.lower()
        rows = [r for r in self.index()
                if r["full_name_norm"] == low and (not pv or r["platform_version"] == pv)]
        if not rows:
            return {"found": False, "name": name, "platform_version": pv or None}
        def _newest(r: dict) -> tuple:
            kind, nums = _pv_key(r["platform_version"])
            return (kind, [-x for x in nums])

        rows.sort(key=_newest)  # newest build wins
        return self._doc(rows[0])

    def search_titles(self, query: str, platform_version: str = "", limit: int = 20) -> dict:
        """Substring search over topic titles (RU/EN). Полнотекст по содержимому — у большого
        сервера (векторы+FTS); здесь только навигация по именам."""
        if not self._files:
            return {"found": False, "error": "Справка платформы не настроена (пути в админке /admin)."}
        q = query.strip().lower()
        pv = platform_version.strip()
        scored: list[tuple[int, dict]] = []
        for r in self.index():
            if pv and r["platform_version"] != pv:
                continue
            title = r["full_name_norm"]
            en = (r["en_name"] or "").lower()
            if q in title or q in en:
                rank = 0 if title.startswith(q) or en.startswith(q) else 1
                scored.append((rank, r))
        scored.sort(key=lambda x: (x[0], x[1]["full_name_norm"]))
        return {
            "query": query,
            "match_count": len(scored),
            "matches": [
                {"fqn": f"platform_help:{r['platform_version']}|{r['title']}", "title": r["title"],
                 "en_name": r["en_name"], "platform_version": r["platform_version"],
                 "help_kind": r["help_kind"]}
                for _rank, r in scored[:limit]
            ],
        }
