"""Platform syntax-assistant help (.hbk) for the lite server — direct reading, no Neo4j.

Deliberately thin over the big pipeline's parsing so the two implementations cannot drift:
path resolution/validation is `sources.hbk.HbkSource.validate()` (bin dir | .hbk file,
version auto-detected from the path), container reading is `sources.hbk_container`, page
parsing is `sources.hbk._parse_page`. The only lite-specific part is WHERE the data lives:
an in-memory name index plus a persisted SQLite FTS5 text index instead of vectorized
`:Document` nodes, with exact pages re-read from the container on demand. Lookup semantics of `docinfo` mirror
`queries.docinfo` (RU / EN / «Объект.Метод», optional version, disambiguation list).

Topic counts may differ slightly from the big server: the index keeps every page with a
title, while ingest also drops pages with empty text."""

from __future__ import annotations

import io
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from html import unescape
from pathlib import Path

from ..sources import hbk_container
from ..sources.hbk import HbkSource, _NAME, _parse_page  # noqa: PLC2701 - shared parsing core
from . import text_search

_H1 = re.compile(rb"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_TEXT_INDEX_SCHEMA = 1


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
    """Configured .hbk files + name and persisted full-text indexes."""

    def __init__(self, index_dir: str | Path | None = None) -> None:
        self.entries: list[dict] = []  # as configured: {"version": str, "path": str}
        self._files: list[tuple[str, str, str]] = []  # resolved (hbk_path, pv, help_kind)
        self._index: list[dict] | None = None  # topic rows (text is in the persisted FTS index)
        self._zips: dict[str, zipfile.ZipFile] = {}
        self._index_dir_override = Path(index_dir) if index_dir is not None else None
        self._text_lock = threading.Lock()

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

    # ------------------------------------------------------------- full-text index

    def _text_index_dir(self) -> Path:
        if self._index_dir_override is not None:
            return self._index_dir_override
        override = os.environ.get("ONEC_LITE_FTS_DIR", "").strip()
        if not override:
            # Lazy import avoids platform_help <-> admin import cycle during module startup.
            try:
                from . import admin as lite_admin

                state = lite_admin.load_state(lite_admin.state_file())
                override = str(state.get("fts_dir") or "")
                if not override:
                    return lite_admin.state_file().parent / "fts"
            except Exception:  # noqa: BLE001 - a broken state file must not break help lookup
                pass
        return Path(override) if override else Path.home() / ".onec-lite" / "fts"

    def _text_files(self, platform_version: str) -> list[tuple[str, str, str]]:
        return [row for row in self._files if not platform_version or row[1] == platform_version]

    def _text_index_path(self, platform_version: str) -> Path:
        identity = json.dumps(
            sorted(self._text_files(platform_version)), ensure_ascii=False
        ).encode("utf-8")
        digest = hashlib.sha1(identity).hexdigest()[:16]
        return self._text_index_dir() / f"platform-{digest}.db"

    def _text_fingerprint(self, platform_version: str) -> str:
        rows = []
        for path, version, kind in sorted(self._text_files(platform_version)):
            try:
                st = Path(path).stat()
                stamp = (st.st_size, st.st_mtime_ns)
            except OSError:
                stamp = (0, 0)
            rows.append((path, version, kind, *stamp))
        return hashlib.sha256(
            json.dumps(rows, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _text_meta(path: Path) -> dict[str, str]:
        try:
            con = sqlite3.connect(str(path))
            try:
                return dict(con.execute("SELECT key, value FROM meta"))
            finally:
                con.close()
        except sqlite3.Error:
            return {}

    def _text_index_ready(self, path: Path, fingerprint: str) -> bool:
        meta = self._text_meta(path)
        return (
            meta.get("schema_version") == str(_TEXT_INDEX_SCHEMA)
            and meta.get("fingerprint") == fingerprint
        )

    def _build_text_index(self, path: Path, fingerprint: str, platform_version: str) -> dict:
        """Build to a temporary DB and atomically publish it; exact help remains readable."""
        with self._text_lock:
            if self._text_index_ready(path, fingerprint):
                return {"built": False, "path": str(path)}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
            try:
                tmp.unlink(missing_ok=True)
                con = sqlite3.connect(str(tmp))
                try:
                    con.executescript(
                        """
                        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                        CREATE VIRTUAL TABLE docs USING fts5(
                            title, en_name, body,
                            platform_version UNINDEXED, help_kind UNINDEXED,
                            hbk UNINDEXED, page UNINDEXED,
                            tokenize='unicode61 remove_diacritics 2'
                        );
                        """
                    )
                    started = time.monotonic()
                    count = 0
                    for row in self.index():
                        if platform_version and row["platform_version"] != platform_version:
                            continue
                        doc = self._doc(row)
                        con.execute(
                            "INSERT INTO docs(title, en_name, body, platform_version, help_kind,"
                            " hbk, page) VALUES(?,?,?,?,?,?,?)",
                            (row["title"], row["en_name"] or "", doc.get("text") or "",
                             row["platform_version"], row["help_kind"], row["hbk"], row["page"]),
                        )
                        count += 1
                    built_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    con.executemany(
                        "INSERT INTO meta(key, value) VALUES(?,?)",
                        [("schema_version", str(_TEXT_INDEX_SCHEMA)),
                         ("fingerprint", fingerprint), ("built_at", built_at),
                         ("topics", str(count))],
                    )
                    con.commit()
                    seconds = round(time.monotonic() - started, 3)
                finally:
                    con.close()
                os.replace(tmp, path)
                return {"built": True, "path": str(path), "topics": count,
                        "seconds": seconds}
            finally:
                tmp.unlink(missing_ok=True)
                # Building touches every archive; do not retain all decompressed ZIPs in RAM.
                self._close_zips()

    def _ensure_text_index(self, platform_version: str) -> tuple[Path, dict]:
        path = self._text_index_path(platform_version)
        fingerprint = self._text_fingerprint(platform_version)
        if self._text_index_ready(path, fingerprint):
            return path, {"built": False, "path": str(path)}
        return path, self._build_text_index(path, fingerprint, platform_version)

    def build_text_indexes(self) -> dict:
        """Build the persisted text index for every configured platform version."""
        started = time.monotonic()
        self.index()  # populate titles once; _build_text_index reuses this catalogue
        versions = sorted({version for _path, version, _kind in self._files}, key=_pv_key)
        results = []
        for version in versions:
            path, build = self._ensure_text_index(version)
            meta = self._text_meta(path)
            results.append({
                "platform_version": version,
                "topics": int(meta.get("topics") or 0),
                "built_now": bool(build.get("built")),
                "path": str(path),
            })
        return {"versions": results, "topics": sum(r["topics"] for r in results),
                "seconds": round(time.monotonic() - started, 3)}

    def _search_text(self, query: str, platform_version: str, limit: int) -> dict | None:
        match = text_search.fts_query(query)
        if not match:
            return None
        try:
            path, build = self._ensure_text_index(platform_version)
            con = sqlite3.connect(str(path))
            try:
                where = "docs MATCH ?"
                args: list = [match]
                if platform_version:
                    where += " AND platform_version = ?"
                    args.append(platform_version)
                # BM25 supplies a cheap candidate set; coverage reranking below keeps a natural
                # question's rare terms together (e.g. HTTP + PATCH + arbitrary method).
                candidate_limit = min(500, max(100, max(1, limit) * 20))
                rows = con.execute(
                    "SELECT title, en_name, platform_version, help_kind,"
                    " snippet(docs, -1, '[', ']', '…', 18),"
                    " bm25(docs, 12.0, 8.0, 1.0, 0.0, 0.0, 0.0, 0.0) AS rank, body"
                    f" FROM docs WHERE {where} ORDER BY rank LIMIT ?",
                    [*args, candidate_limit],
                ).fetchall()
                rows.sort(key=lambda row: self._text_rerank_key(row, query), reverse=True)
                rows = rows[:max(1, limit)]
                total = con.execute(f"SELECT count(*) FROM docs WHERE {where}", args).fetchone()[0]
                meta = dict(con.execute("SELECT key, value FROM meta"))
            finally:
                con.close()
        except sqlite3.Error:
            return None  # sqlite without FTS5/corrupt index: exact title search still works
        return {
            "query": query,
            "match_count": total,
            "index": {"built_at": meta.get("built_at"), "topics": int(meta.get("topics") or 0),
                      "built_now": bool(build.get("built")),
                      **({"build_seconds": build["seconds"]} if build.get("built") else {})},
            "matches": [
                {"fqn": f"platform_help:{pv}|{title}", "title": title, "en_name": en,
                 "platform_version": pv, "help_kind": kind, "snippet": snippet,
                 "score": round(-rank, 3)}
                for title, en, pv, kind, snippet, rank, _body in rows
            ],
        }

    @staticmethod
    def _text_rerank_key(row: tuple, query: str) -> tuple:
        terms = text_search.query_terms(query)
        title_hits = text_search.term_coverage(terms, row[0], row[1])
        coverage = text_search.term_coverage(terms, row[0], row[1], row[6])
        q = query.strip().lower()
        exact_title = int(q in {str(row[0] or "").lower(), str(row[1] or "").lower()})
        bm25_score = -float(row[5] or 0.0)
        # Coverage is a nudge, not a hard tier: making it lexicographic promoted long pages
        # which happened to repeat every generic word above a concise, correct API topic.
        blended = bm25_score + coverage * 2.0 + title_hits
        return exact_title, blended, coverage, title_hits

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
        """Ranked FTS5 search over RU/EN titles and full topic text, version-aware.

        The persisted index is built lazily from the configured .hbk files.  If FTS5 is not
        available, the old title-substring search remains as a safe fallback.
        """
        if not self._files:
            return {"found": False, "error": "Справка платформы не настроена (пути в админке /admin)."}
        pv = platform_version.strip()
        fulltext = self._search_text(query, pv, limit)
        if fulltext is not None:
            return fulltext
        q = query.strip().lower()
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
