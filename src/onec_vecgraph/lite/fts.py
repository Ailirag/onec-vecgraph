"""SQLite FTS5 (BM25) index over the working copy: ranked search without ML.

Opt-in layer between substring rg and the big server's vectors: units are BSL routines
(title = routine, tokens = CamelCase sub-words via the shared `chunking.search_tokens`,
body = source) and object cards (name/synonym/attribute names). BM25 column weights
favour identifier hits over body mentions; Cyrillic query tokens get a trimmed prefix
alternative («себестоимости» ↔ «Себестоимость» без стеммера).

Consistency by design:
  * the DB lives OUTSIDE the working copy (~/.onec-lite/fts/<digest>.db) — a developer's
    `git status` never sees it;
  * built explicitly (admin button / CLI --build-fts), refreshed incrementally by file
    mtime; searches auto-refresh at most every _REFRESH_TTL seconds and always report
    `built_at`, so staleness is bounded and visible, never silent;
  * FTS5 availability is feature-detected — without it the tool degrades to a clear
    message (rg search keeps working).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from ..chunking import search_tokens
from . import admin as lite_admin
from . import code_intel
from .workspace import LiteSource, Workspace, read_text

_SCHEMA_VERSION = 2  # v2: title = токены имени (иначе CamelCase-имя не матчится), +display
_REFRESH_TTL = 30.0  # seconds between implicit mtime rescans on search
_BODY_CAP = 20_000  # per-unit body cap (защита от патологических рутин)
_CYR = re.compile(r"[а-яё]", re.IGNORECASE)


def fts_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
            return True
        finally:
            con.close()
    except sqlite3.OperationalError:
        return False


def db_path_for(ws: Workspace) -> Path:
    digest = hashlib.sha1(str(ws.root).lower().encode("utf-8")).hexdigest()[:16]
    return lite_admin.state_file().parent / "fts" / f"{digest}.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    if ver == _SCHEMA_VERSION:
        return
    con.executescript(
        """
        DROP TABLE IF EXISTS units;
        DROP TABLE IF EXISTS unit_map;
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS meta;
        CREATE VIRTUAL TABLE units USING fts5(
            title, tokens, body,
            display UNINDEXED, unit UNINDEXED, source UNINDEXED, object UNINDEXED,
            path UNINDEXED, line UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE unit_map(path TEXT NOT NULL, rowid_ref INTEGER NOT NULL);
        CREATE INDEX unit_map_path ON unit_map(path);
        CREATE TABLE files(path TEXT PRIMARY KEY, mtime REAL NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
    con.commit()


# --------------------------------------------------------------------------- #
# Unit extraction
# --------------------------------------------------------------------------- #

def _bsl_units(ws: Workspace, src: LiteSource, path: Path) -> list[tuple]:
    src_name, rel = ws.source_of_path(path)
    descr = code_intel.describe_bsl_path(src, rel)
    text = read_text(path)
    lines = text.splitlines()
    rows: list[tuple] = []
    for rt in code_intel.routines_of(path):
        body = "\n".join(lines[rt.start_line - 1 : rt.end_line])[:_BODY_CAP]
        tokens = search_tokens(rt.name, descr.get("object"), descr.get("module"))
        rows.append((
            search_tokens(rt.name), tokens, body,
            rt.name, "routine", src_name, descr.get("object", ""), rel, rt.start_line,
        ))
    return rows


def _object_units(ws: Workspace, src: LiteSource, ref) -> list[tuple]:
    fqn, meta, _obj_dir = ref
    try:
        obj = ws.parse_object(src, ref)
    except ValueError:
        return []
    parts: list[str] = [obj.synonym or ""]
    token_srcs: list[str | None] = [obj.kind, obj.name, obj.synonym]
    for f in obj.fields:
        parts.append(f"{f.name} {f.synonym}".strip())
        token_srcs += [f.name, f.synonym]
    for ts in obj.tabular:
        parts.append(f"{ts.name} {ts.synonym}".strip())
        token_srcs += [ts.name, ts.synonym]
        for f in ts.fields:
            parts.append(f.name)
            token_srcs.append(f.name)
    for v in obj.enum_values:
        parts.append(f"{v.name} {v.synonym}".strip())
        token_srcs += [v.name, v.synonym]
    _src_name, rel = ws.source_of_path(meta)
    return [(
        search_tokens(obj.kind, obj.name, obj.synonym), search_tokens(*token_srcs),
        "\n".join(p for p in parts if p)[:_BODY_CAP],
        fqn, "object", src.name, fqn, rel, 1,
    )]


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

class FtsIndex:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.path = db_path_for(ws)
        self._last_refresh = 0.0

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        out: dict = {"available": fts_available(), "db": str(self.path), "built": False}
        if not self.path.is_file():
            return out
        try:
            con = _connect(self.path)
            try:
                if con.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                    return out
                meta = dict(con.execute("SELECT key, value FROM meta"))
                units = con.execute("SELECT count(*) FROM unit_map").fetchone()[0]
                files = con.execute("SELECT count(*) FROM files").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            return out
        out.update(built=True, built_at=meta.get("built_at"), units=units, files=files,
                   size_bytes=self.path.stat().st_size)
        return out

    # -- build / refresh ------------------------------------------------------

    def build(self) -> dict:
        """Incremental build by mtime (first run indexes everything)."""
        if not fts_available():
            return {"error": "FTS5 недоступен в этой сборке Python (sqlite3 без fts5)."}
        t0 = time.monotonic()
        con = _connect(self.path)
        try:
            _ensure_schema(con)
            known: dict[str, float] = {
                p: m for p, m in con.execute("SELECT path, mtime FROM files")
            }
            seen: set[str] = set()
            added = updated = removed = units_written = 0

            def reindex(abs_path: Path, kind: str, rows: list[tuple], mtime: float) -> None:
                nonlocal units_written
                key = str(abs_path)
                for (rid,) in con.execute(
                    "SELECT rowid_ref FROM unit_map WHERE path=?", (key,)
                ):
                    con.execute("DELETE FROM units WHERE rowid=?", (rid,))
                con.execute("DELETE FROM unit_map WHERE path=?", (key,))
                for row in rows:
                    cur = con.execute(
                        "INSERT INTO units(title, tokens, body, display, unit, source,"
                        " object, path, line) VALUES(?,?,?,?,?,?,?,?,?)", row,
                    )
                    con.execute("INSERT INTO unit_map(path, rowid_ref) VALUES(?,?)",
                                (key, cur.lastrowid))
                    units_written += 1
                con.execute(
                    "INSERT OR REPLACE INTO files(path, mtime, kind) VALUES(?,?,?)",
                    (key, mtime, kind),
                )

            # fresh=True: TTL-кэши списков скрыли бы удалённые файлы; исчезнувший между
            # листингом и stat() файл не попадает в seen -> его подметёт свип удалений.
            for s in self.ws.sources:
                for p in self.ws.bsl_files(s, fresh=True):
                    key = str(p)
                    try:
                        mtime = p.stat().st_mtime
                    except OSError:
                        continue
                    seen.add(key)
                    if known.get(key) == mtime:
                        continue
                    reindex(p, "bsl", _bsl_units(self.ws, s, p), mtime)
                    added += 1 if key not in known else 0
                    updated += 1 if key in known else 0
                for ref in self.ws.listing(s, fresh=True):
                    meta_path = ref[1]
                    key = str(meta_path)
                    try:
                        mtime = meta_path.stat().st_mtime
                    except OSError:
                        continue
                    seen.add(key)
                    if known.get(key) == mtime:
                        continue
                    reindex(meta_path, "meta", _object_units(self.ws, s, ref), mtime)
                    added += 1 if key not in known else 0
                    updated += 1 if key in known else 0

            for gone in set(known) - seen:
                for (rid,) in con.execute(
                    "SELECT rowid_ref FROM unit_map WHERE path=?", (gone,)
                ):
                    con.execute("DELETE FROM units WHERE rowid=?", (rid,))
                con.execute("DELETE FROM unit_map WHERE path=?", (gone,))
                con.execute("DELETE FROM files WHERE path=?", (gone,))
                removed += 1

            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('built_at', ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('root', ?)",
                (json.dumps(str(self.ws.root), ensure_ascii=False),),
            )
            con.commit()
        finally:
            con.close()
        self._last_refresh = time.monotonic()
        return {
            "files_added": added, "files_updated": updated, "files_removed": removed,
            "units_written": units_written, "seconds": round(time.monotonic() - t0, 1),
            **{k: v for k, v in self.status().items() if k in ("units", "files")},
        }

    def _maybe_refresh(self) -> None:
        # >=: на Windows time.monotonic() тикает ~раз в 15.6 мс — строгое «>» с TTL=0
        # пропускало обновление, если поиск случался внутри одного кванта таймера.
        if time.monotonic() - self._last_refresh >= _REFRESH_TTL:
            self.build()

    # -- search ---------------------------------------------------------------

    def search(self, query: str, *, limit: int = 20, unit: str = "", source: str = "") -> dict:
        if not fts_available():
            return {"error": "FTS5 недоступен в этой сборке Python (sqlite3 без fts5)."}
        if not self.path.is_file():
            return {"error": "Индекс поиска не построен: кнопка «Построить индекс поиска» "
                             "в админке или serve-lite --build-fts."}
        match = _fts_query(query)
        if not match:
            return {"error": "Пустой запрос."}
        self._maybe_refresh()
        con = _connect(self.path)
        try:
            _ensure_schema(con)
            where = "units MATCH ?"
            args: list = [match]
            if unit:
                where += " AND unit = ?"
                args.append(unit)
            if source:
                where += " AND source = ?"
                args.append(source)
            args.append(max(1, limit))
            rows = con.execute(
                "SELECT display, unit, source, object, path, line,"
                "       snippet(units, 2, '[', ']', '…', 12) AS snip,"
                "       bm25(units, 10.0, 5.0, 1.0) AS rank"
                f" FROM units WHERE {where} ORDER BY rank LIMIT ?",
                args,
            ).fetchall()
            built_at = dict(con.execute("SELECT key, value FROM meta")).get("built_at")
        except sqlite3.OperationalError as exc:
            return {"error": f"Ошибка запроса FTS: {exc}"}
        finally:
            con.close()
        return {
            "query": query,
            "match": match,
            "built_at": built_at,
            "match_count": len(rows),
            "results": [
                {
                    "title": r[0], "unit": r[1], "source": r[2], "object": r[3],
                    "path": r[4], "line": r[5], "snippet": r[6],
                    "score": round(-r[7], 3),  # bm25: меньше = лучше; наружу — больше = лучше
                }
                for r in rows
            ],
        }


def _fts_query(query: str) -> str:
    """User text -> FTS5 MATCH: CamelCase-подслова, кириллице добавляется усечённый
    префикс-вариант («себестоимости» -> (себестоимости OR себестоимост*))."""
    tokens = search_tokens(query).split()
    parts: list[str] = []
    for tok in tokens:
        safe = tok.replace('"', " ").strip()
        if not safe:
            continue
        if _CYR.search(safe) and len(safe) >= 6:
            parts.append(f'("{safe}" OR "{safe[:-2]}"*)')
        else:
            parts.append(f'"{safe}"')
    # OR + BM25: юниты с бОльшим числом совпавших термов ранжируются выше сами, а
    # AND-семантика губит NL-запросы («где считается X» — слова «где» в коде нет).
    return " OR ".join(parts)


# Per-workspace index cache (keyed by root; the Workspace object may be re-created).
_INDEXES: dict[str, FtsIndex] = {}


def index_for(ws: Workspace) -> FtsIndex:
    key = str(ws.root).lower()
    idx = _INDEXES.get(key)
    if idx is None or idx.ws is not ws:
        idx = FtsIndex(ws)
        _INDEXES[key] = idx
    return idx
