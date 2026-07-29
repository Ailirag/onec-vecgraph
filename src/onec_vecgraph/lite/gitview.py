"""Git-awareness over the live working copy: what changed and what the change touches.

The unique lite capability (the big server indexes snapshots and cannot see uncommitted
work): map `git status`/`git diff` onto metadata objects, then map changed hunk lines
onto BSL routines and pull their callers — a ready review packet for a branch.

Only read-only git commands are used (rev-parse/status/diff/ls-files) via the system
`git`, same precedent as sources/git_repo. A non-git working copy yields a clear error.
"""

from __future__ import annotations

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from ..chunking import classify_entry_point
from ..parsing.dump import TYPE_FOLDERS
from . import code_intel
from .workspace import LiteSource, Workspace

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MAX_ROUTINES = 60  # cap on routines expanded with callers in one review_set


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            # quotepath=off: иначе git экранирует кириллицу окталями и пути не мапятся.
            # safe.directory=*: рабочая копия часто смонтирована в контейнер (serve-lite в
            # Docker, ro-монт) и принадлежит другому uid, чем процесс сервера — без этого git
            # на ЛЮБОЙ команде отвечает "detected dubious ownership" и отказывает. Команды
            # здесь только читающие, репозиторий доверенный (примонтирован оператором).
            ["git", "-c", "core.quotepath=off", "-c", "safe.directory=*", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=120,
        )
    except FileNotFoundError:
        return 127, "git не найден в PATH"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)


@lru_cache(maxsize=64)
def _repo_root(path: Path) -> tuple[Path | None, str]:
    """(git toplevel, "") при успехе; (None, <сообщение git>) иначе. Ищет .git вверх по
    дереву от path, поэтому источник в подпапке (…/conf/src) с .git уровнем выше — корректно.

    Кэш по пути: у воркспейса 5+ источников обычно в ОДНОМ репозитории, а каждый вызов git на
    Windows — сотни миллисекунд; без кэша это лишний git-процесс на каждый источник на каждый
    вызов тула (changed_objects/review_set)."""
    code, out = _git(["rev-parse", "--show-toplevel"], path)
    if code == 0 and out.strip():
        return Path(out.strip()), ""
    return None, out.strip()


def _branch(root: Path) -> str:
    code, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return out.strip() if code == 0 else ""


def _repos(sources: list[LiteSource]) -> tuple[dict[Path, list[LiteSource]], list[dict]]:
    """Group sources by their git repo root; report sources outside any repo."""
    by_root: dict[Path, list[LiteSource]] = {}
    missing: list[dict] = []
    for s in sources:
        root, err = _repo_root(s.files_root)
        if root is None:
            missing.append({
                "source": s.name, "root": str(s.root),
                "error": "git по источнику недоступен: не рабочая копия под git, git не "
                         "установлен, или каталог с чужим владельцем (dubious ownership)."
                         + (f" git: {err}" if err else ""),
            })
        else:
            by_root.setdefault(root, []).append(s)
    return by_root, missing


def _status_files(repo: Path) -> tuple[list[tuple[str, str]], str | None]:
    """(status, repo-relative path) for worktree+index changes incl. untracked."""
    code, out = _git(["status", "--porcelain"], repo)
    if code != 0:
        return [], out.strip()
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        status, rest = line[:2].strip() or "??", line[3:]
        if " -> " in rest:  # rename: учитываем новую сторону
            rest = rest.split(" -> ", 1)[1]
        rows.append((status, rest.strip().strip('"')))
    return rows, None


def _diff_files(repo: Path, ref: str, *, untracked: bool = False) -> tuple[list[tuple[str, str]], str | None]:
    """Изменения против ref. untracked=True добавляет неотслеживаемые файлы.

    Обход untracked стоит дороже самого диффа (на УТ ~1.4 с против ~0.4 с) и для вопроса
    «что изменилось относительно ref» обычно не нужен, поэтому по умолчанию выключен; когда
    он запрошен — идёт параллельно диффу (стена = максимум, а не сумма)."""
    if not untracked:
        code, out = _git(["diff", "--name-status", "--find-renames", ref, "--", "."], repo)
        code2, out2 = 1, ""
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_diff = pool.submit(
                _git, ["diff", "--name-status", "--find-renames", ref, "--", "."], repo)
            fut_new = pool.submit(_git, ["ls-files", "--others", "--exclude-standard"], repo)
            code, out = fut_diff.result()
            code2, out2 = fut_new.result()
    if code != 0:
        return [], out.strip()
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0][:1], parts[-1].strip()))
    if code2 == 0:
        rows.extend(("??", p.strip()) for p in out2.splitlines() if p.strip())
    return rows, None


_FILE_LIST_TTL = 60.0  # с: агент часто зовёт changed_objects и review_set подряд
_FILE_LISTS: dict[tuple[str, str, bool], tuple[float, list[tuple[str, str]], str | None]] = {}


def _file_list(repo: Path, ref: str, *, untracked: bool = False) -> tuple[list[tuple[str, str]], str | None]:
    """git-список изменённых файлов с коротким TTL-кэшем.

    Один и тот же набор нужен и changed_objects, и review_set (и агент обычно вызывает их
    подряд) — а git diff + обход untracked на УТ это ~1.8 с. Кэш живёт секунды, поэтому
    свежесть рабочей копии практически не страдает."""
    key = (str(repo), ref, untracked)
    now = time.monotonic()
    hit = _FILE_LISTS.get(key)
    if hit and now - hit[0] < _FILE_LIST_TTL:
        return hit[1], hit[2]
    # ref пуст -> git status --porcelain и так включает untracked, доп. обход не нужен
    files, err = _diff_files(repo, ref, untracked=untracked) if ref else _status_files(repo)
    _FILE_LISTS[key] = (now, files, err)
    return files, err


_artifact = code_intel.artifact_of


def _map_changes(ws: Workspace, sources: list[LiteSource], ref: str,
                 *, untracked: bool = False):
    """Yield (source, abs_path, source-relative path, status) for files inside kind folders."""
    by_root, missing = _repos(sources)
    repos_info: list[dict] = list(missing)
    rows: list[tuple[LiteSource, Path, str, str]] = []
    wanted = {s.name for s in sources}
    for repo, _repo_sources in by_root.items():
        files, err = _file_list(repo, ref, untracked=untracked)
        repos_info.append({"repo": str(repo), "branch": _branch(repo),
                           **({"error": err} if err else {"files": len(files)})})
        if err:
            continue
        for status, rel in files:
            abs_path = repo / rel
            src_name, srel = ws.source_of_path(abs_path)
            if not src_name or src_name not in wanted:
                continue
            if srel.split("/", 1)[0] not in TYPE_FOLDERS:
                continue  # DT-INF, docs и прочее вне метаданных
            src = next(s for s in ws.sources if s.name == src_name)
            rows.append((src, abs_path, srel, status))
    return rows, repos_info


def changed_objects(ws: Workspace, ref: str = "", source: str = "",
                    include_untracked: bool = True) -> dict:
    """Изменённые объекты рабочей копии: git status (или diff против ref) → объекты.

    ref пуст — незакоммиченные изменения (staged+unstaged+untracked, они уже в git status);
    иначе — против ref (ветка/коммит/HEAD~1) ПЛЮС неотслеживаемые файлы. Обход untracked —
    отдельный проход по дереву (на большой выгрузке ~1.4 с), но без него из ревью молча
    исчезают только что созданные модули, поэтому по умолчанию он включён;
    include_untracked=False убирает его, когда важна скорость, а не полнота."""
    sources, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    rows, repos_info = _map_changes(ws, sources, ref, untracked=include_untracked)
    objects = _group_objects(rows)
    return {
        "ref": ref or "(незакоммиченные изменения)",
        "repos": repos_info,
        "object_count": len(objects),
        "objects": objects,
    }


def _group_objects(rows: list[tuple[LiteSource, Path, str, str]]) -> list[dict]:
    """Изменённые файлы -> объекты метаданных. Общая часть changed_objects и review_set,
    чтобы review_set не повторял весь git-проход второй раз (на УТ это ~1.8 с впустую)."""
    grouped: dict[tuple[str, str], dict] = {}
    for src, _abs_path, srel, status in rows:
        descr = code_intel.describe_bsl_path(src, srel)
        key = (src.name, descr["object"])
        g = grouped.setdefault(key, {
            "source": src.name, "object": descr["object"], "kind": descr["kind"],
            "changes": [],
        })
        g["changes"].append({"path": srel, "status": status, "artifact": _artifact(srel),
                             "module": descr["module"] if srel.endswith(".bsl") else None})
    return sorted(grouped.values(), key=lambda g: (g["source"], g["object"]))


_DIFF_HEAD_RE = re.compile(r'^\+\+\+ (?:b/)?(.+)$')


def _touched_ranges_bulk(repo: Path, ref: str) -> dict[str, list[tuple[int, int]]] | None:
    """{repo-relative путь: диапазоны изменённых строк} ОДНИМ `git diff -U0` на весь набор.

    Раньше review_set делал отдельный git-процесс на каждый изменённый файл (13 файлов ≈ 0.9 с
    только на запуск процессов); один общий дифф отдаёт то же за ~0.45 с."""
    code, out = _git(["diff", "-U0", ref or "HEAD", "--", "."], repo)
    if code != 0:
        return None
    per_file: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in out.splitlines():
        head = _DIFF_HEAD_RE.match(line)
        if head:
            path = head.group(1).strip().strip('"')
            current = None if path == "/dev/null" else path
            if current:
                per_file.setdefault(current, [])
            continue
        m = _HUNK_RE.match(line)
        if m and current:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            per_file[current].append((start, start + max(count, 1) - 1))
    return per_file


def review_set(ws: Workspace, ref: str = "", max_callers: int = 5, source: str = "",
               detail: bool = False, max_routines: int = 25) -> dict:
    """Ревью-набор: изменённые строки → затронутые рутины → их вызывающие и override-хуки.

    Для каждого изменённого .bsl: ханки `git diff -U0` мапятся на текущие рутины файла;
    untracked-модули включаются целиком. Каждая затронутая рутина получает callers
    (парсер-верифицировано) и, для экспортных/заимствованных, — override-хуки расширений.

    По умолчанию вызывающие отдаются компактными строками `Объект▸Модуль▸Рутина:строка`
    (кратно дешевле по токенам при той же информации); detail=True — полные записи."""
    sources, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    rows, repos_info = _map_changes(ws, sources, ref)
    changed_list = _group_objects(rows)  # из своих же rows — без второго git-прохода

    # Полный детерминированный индекс переопределений (TTL-кэш): раньше здесь был скан с
    # обрезкой по 300 файлам в недетерминированном порядке — переопределения изменённых
    # рутин могли молча не попасть в ревью-набор.
    all_overrides = code_intel.override_index(ws)
    by_target: dict[str, list[dict]] = {}
    for o in all_overrides:
        target = (o.get("target") or "").lower()
        if target:
            by_target.setdefault(target, []).append(o)

    # Проход 1: какие рутины затронуты (без поиска вызывающих).
    routines: list[dict] = []
    truncated = False
    hints: dict[str, str] = {}
    bulk_ranges: dict[Path, dict[str, list[tuple[int, int]]] | None] = {}
    for src, abs_path, srel, status in rows:
        if not srel.endswith(".bsl"):
            continue
        repo, _ = _repo_root(src.files_root)
        if repo is None:
            continue
        rel_in_repo = str(abs_path.relative_to(repo)).replace("\\", "/")
        if repo not in bulk_ranges:
            bulk_ranges[repo] = _touched_ranges_bulk(repo, ref)
        per_file = bulk_ranges[repo]
        ranges = None if status == "??" or per_file is None else per_file.get(rel_in_repo, [])
        parsed = code_intel.routines_of(abs_path)
        if ranges is None:
            touched = list(parsed)  # новый файл: затронуто всё
        else:
            touched = [rt for rt in parsed
                       if any(rt.start_line <= hi and rt.end_line >= lo for lo, hi in ranges)]
        descr = code_intel.describe_bsl_path(src, srel)
        for rt in touched:
            if len(routines) >= _MAX_ROUTINES:
                truncated = True
                break
            hint = descr["object"].partition(".")[2] if descr["kind"] == "CommonModule" else ""
            if hint:
                hints.setdefault(rt.name, hint)
            routines.append({
                "source": src.name,
                "object": descr["object"],
                "module": descr["module"],
                "routine": rt.name,
                "lines": [rt.start_line, rt.end_line],
                "export": rt.export,
                "entry_point": classify_entry_point(rt.name),
                "status": status,
                "overridden_by": [
                    {k: o.get(k) for k in ("source", "object", "routine", "mode")}
                    if detail else f"{o.get('source')}▸{o.get('routine')}[{o.get('mode')}]"
                    for o in by_target.get(rt.name.lower(), [])
                    if (o.get("object") or "").lower() == descr["object"].lower()
                ],
            })
        if truncated:
            break

    # Проход 2: вызывающие для ВСЕХ затронутых рутин одной выборкой (индекс) или одним
    # текстовым сканом (фолбэк) — было по скану на каждую рутину.
    batch = code_intel.find_callers_batch(
        ws, sorted({r["routine"] for r in routines}), hints=hints,
        max_per_name=max_callers, source=source,
    )
    for row in routines:
        found = batch.get(row["routine"], [])
        row["callers_count"] = len(found)
        if detail:
            row["callers"] = found
        else:
            # Компактная форма: одна строка на вызывающего вместо словаря из восьми полей —
            # на ревью-наборе это кратно меньше токенов при той же полезной информации.
            row["callers"] = [
                f"{c.get('object')}▸{c.get('module')}▸{c.get('routine')}"
                + (f":{c['call_line']}" if c.get("call_line") else "")
                for c in found
            ]
        row["callers_truncated"] = len(found) >= max_callers

    # Бюджет ответа: рутин в наборе бывает много (десятки), и агенту важны прежде всего
    # рискованные — экспортные, точки входа и те, у кого много вызывающих или есть
    # переопределения. Сортируем по риску и отдаём max_routines, сообщая полный счёт.
    routines_total = len(routines)
    if not detail and routines_total > max_routines:
        def risk(r: dict) -> tuple:
            return (bool(r.get("export")), bool(r.get("entry_point")),
                    len(r.get("overridden_by") or []), r.get("callers_count", 0))
        routines = sorted(routines, key=risk, reverse=True)[:max_routines]
    if not detail:
        for r in routines:  # переопределений у популярных хуков бывают десятки
            ov = r.get("overridden_by") or []
            if len(ov) > 3:
                r["overridden_by"] = ov[:3]
                r["overridden_by_total"] = len(ov)
        # Компактный список изменённых объектов: «Объект: путь(статус), …» вместо словарей —
        # рутины ниже и так несут детали, а метаданные-правки остаются видимыми.
        changed_list = [
            {"source": g["source"], "object": g["object"],
             "changes": [f"{c['path']}({c['status']})" for c in g["changes"]]}
            for g in changed_list
        ]
    return {
        "ref": ref or "(незакоммиченные изменения)",
        "repos": repos_info,
        "changed_objects": changed_list,
        "routine_count": routines_total,
        "routines_returned": len(routines),
        "routines_ranked_by_risk": routines_total > len(routines),
        "routines_truncated": truncated,
        "routines": routines,
    }
