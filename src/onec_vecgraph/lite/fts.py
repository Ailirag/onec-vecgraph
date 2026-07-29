"""SQLite FTS5 (BM25) index over the working copy: ranked search without ML.

Opt-in layer between substring rg and the big server's vectors: units are BSL routines
(title = routine, tokens = CamelCase sub-words via the shared `chunking.search_tokens`,
body = source) and object cards (name/synonym/attribute names). BM25 column weights
favour identifier hits over body mentions; Cyrillic query tokens get a trimmed prefix
alternative («себестоимости» ↔ «Себестоимость» без стеммера).

Consistency by design:
  * the DB lives OUTSIDE the working copy (~/.onec-lite/fts/<digest>.db) — a developer's
    `git status` never sees it;
  * built in the background (kicked on workspace load, HTTP prebuild-all, admin button or
    CLI --build-fts) and refreshed incrementally by file mtime; a single writer at a time
    (the `_building` flag) never blocks searches; while the first build is in flight
    `search()` returns `ready=false` so the caller degrades to rg instead of silent
    emptiness; refresh is kicked at most every _REFRESH_TTL seconds and `built_at` is
    reported, so staleness is bounded and visible;
  * FTS5 availability is feature-detected — without it the tool degrades to a clear
    message (rg search keeps working).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
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
_BUILD_LOCK_STALE = 3600.0  # сек: лок сборки старше — осиротел (процесс умер), забираем
_BUILD_WAIT = 900.0  # сек: сколько синхронный build() ждёт уже идущую сборку


def _acquire_build_lock(db_path: Path):
    """Межпроцессный лок сборки FTS: атомарный файл `<db>.building`. Возвращает handle или
    None, если индекс уже строит ДРУГОЙ процесс. Нужен потому, что каталог ~/.onec-lite/fts
    общий — два сервера (напр. stdio + http) иначе дерутся за один файл БД (блокировки/зависания)."""
    lock = Path(str(db_path) + ".building")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime <= _BUILD_LOCK_STALE:
                return None  # свежий лок — строит другой процесс
            lock.unlink()  # осиротевший — забираем
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (OSError, FileExistsError):
            return None
    try:
        os.write(fd, f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}".encode("utf-8"))
    except OSError:
        pass
    return fd, lock


def _release_build_lock(handle) -> None:
    if not handle:
        return
    fd, lock = handle
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock.unlink()
    except OSError:
        pass


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
        self._build_lock = threading.Lock()  # CAS флага _building (держится кратко)
        self._building = False                # идёт ли сборка (fg или bg) — единств. писатель

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        out: dict = {"available": fts_available(), "db": str(self.path),
                     "built": False, "building": self._building}
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

    def _try_start_build(self, *, force: bool) -> bool:
        """CAS: занять флаг «строится», если сборка не идёт и (force | истёк TTL).
        _build_lock держится лишь на время проверки, не на время самой сборки."""
        with self._build_lock:
            if self._building:
                return False
            if not force and time.monotonic() - self._last_refresh < _REFRESH_TTL:
                return False
            self._building = True
            return True

    def ensure_background(self, *, force: bool = False) -> bool:
        """Неблокирующе запустить инкрементальную сборку в daemon-потоке (если она не идёт
        и force|истёк TTL); поиск при этом работает по текущему индексу. Возвращает True,
        если сборка запущена этим вызовом."""
        if not fts_available() or not self._try_start_build(force=force):
            return False

        def _run() -> None:
            try:
                self._build_locked()
            except Exception:  # noqa: BLE001 — фон не должен ронять сервер
                logging.getLogger(__name__).exception("fts: фоновая сборка упала (%s)", self.path)
            finally:
                with self._build_lock:
                    self._building = False

        threading.Thread(target=_run, name="fts-build", daemon=True).start()
        return True

    def _wait_not_building(self, timeout: float) -> bool:
        """Дождаться конца сборки, идущей в ЭТОМ процессе. True — флаг свободен."""
        deadline = time.monotonic() + timeout
        while True:
            with self._build_lock:
                if not self._building:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def build(self, *, wait: float = _BUILD_WAIT) -> dict:
        """Синхронная сборка (кнопка админки / --build-fts): по возвращении индекс СОБРАН.

        Если сборка уже идёт (фоновый прогрев или другой процесс) — не встаём вторым
        писателем, а ДОЖИДАЕМСЯ её (ограниченно по времени): вызывающий вправе считать, что
        после build() без ошибки индекс готов. wait=0 — не ждать (для не-блокирующих путей)."""
        if not fts_available():
            return {"error": "FTS5 недоступен в этой сборке Python (sqlite3 без fts5)."}
        if not self._try_start_build(force=True):
            # строится в этом процессе: ждём готовности вместо второго писателя
            if self._wait_not_building(wait) and self._built_at():
                return {"status": "built_by_background",
                        "note": "Сборку завершил фоновый прогрев.",
                        **{k: v for k, v in self.status().items() if k in ("units", "files")}}
            return {"status": "building", "note": "Сборка индекса ещё идёт; повторите позже."}
        try:
            return self._build_locked(wait=wait)
        finally:
            with self._build_lock:
                self._building = False

    def _build_locked(self, *, wait: float = 0.0) -> dict:
        """Единственный писатель В ЭТОМ процессе гарантирован флагом _building; плюс
        МЕЖПРОЦЕССНЫЙ лок (файл `<db>.building`): если индекс строит другой процесс (общий
        каталог ~/.onec-lite/fts), не деремся за один файл БД (иначе блокировки SQLite /
        зависания вызовов) — фоновый путь (wait=0) сразу уступает, синхронный build() ждёт."""
        lock = _acquire_build_lock(self.path)
        if lock is None and wait > 0:
            deadline = time.monotonic() + wait
            while lock is None and time.monotonic() < deadline:
                time.sleep(0.2)
                lock = _acquire_build_lock(self.path)
        if lock is None:
            self._last_refresh = time.monotonic()  # строит другой процесс — отступаем по TTL
            return {"status": "building",
                    "note": "Индекс строит другой процесс — пропускаю (общий каталог fts)."}
        try:
            return self._run_build()
        finally:
            _release_build_lock(lock)

    def _run_build(self) -> dict:
        """Incremental build by mtime (первый прогон индексирует всё). Единственный писатель
        гарантирован _building (в процессе) и файловым локом (между процессами)."""
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

    def _built_at(self) -> str | None:
        """Время последней ЗАВЕРШЁННОЙ сборки (meta.built_at пишется одним коммитом в конце)
        или None: файла нет / схема устарела / первая сборка ещё не докоммичена."""
        if not self.path.is_file():
            return None
        try:
            con = _connect(self.path)
            try:
                if con.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                    return None
                row = con.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
                return row[0] if row else None
            finally:
                con.close()
        except sqlite3.Error:
            return None

    # -- search ---------------------------------------------------------------

    def search(self, query: str, *, limit: int = 20, unit: str = "", source: str = "") -> dict:
        if not fts_available():
            return {"error": "FTS5 недоступен в этой сборке Python (sqlite3 без fts5).",
                    "fts_available": False, "ready": False}
        match = _fts_query(query)
        if not match:
            return {"error": "Пустой запрос."}
        if self._built_at() is None:
            # индекс ещё не построен/не докоммичен: запускаем фон и честно сообщаем «не готов»,
            # чтобы вызывающий (fts_search) прозрачно деградировал на rg, а не отдал пусто.
            self.ensure_background(force=True)
            return {"query": query, "ready": False, "building": self._building,
                    "note": "Индекс поиска строится в фоне — используйте search_code или "
                            "повторите запрос чуть позже."}
        self.ensure_background()  # неблокирующий инкрементальный рефреш по TTL (в фоне)
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
            "ready": True,
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
