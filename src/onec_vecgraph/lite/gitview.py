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
            # quotepath=off: иначе git экранирует кириллицу окталями и пути не мапятся
            ["git", "-c", "core.quotepath=off", *args],
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


def _repo_root(path: Path) -> Path | None:
    code, out = _git(["rev-parse", "--show-toplevel"], path)
    return Path(out.strip()) if code == 0 and out.strip() else None


def _branch(root: Path) -> str:
    code, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return out.strip() if code == 0 else ""


def _repos(sources: list[LiteSource]) -> tuple[dict[Path, list[LiteSource]], list[dict]]:
    """Group sources by their git repo root; report sources outside any repo."""
    by_root: dict[Path, list[LiteSource]] = {}
    missing: list[dict] = []
    for s in sources:
        root = _repo_root(s.files_root)
        if root is None:
            missing.append({"source": s.name, "root": str(s.root),
                            "error": "не git-репозиторий (или git не найден)"})
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


def _diff_files(repo: Path, ref: str) -> tuple[list[tuple[str, str]], str | None]:
    code, out = _git(["diff", "--name-status", "--find-renames", ref, "--", "."], repo)
    if code != 0:
        return [], out.strip()
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0][:1], parts[-1].strip()))
    code2, out2 = _git(["ls-files", "--others", "--exclude-standard"], repo)
    if code2 == 0:
        rows.extend(("??", p.strip()) for p in out2.splitlines() if p.strip())
    return rows, None


_artifact = code_intel.artifact_of


def _map_changes(ws: Workspace, sources: list[LiteSource], ref: str):
    """Yield (source, abs_path, source-relative path, status) for files inside kind folders."""
    by_root, missing = _repos(sources)
    repos_info: list[dict] = list(missing)
    rows: list[tuple[LiteSource, Path, str, str]] = []
    wanted = {s.name for s in sources}
    for repo, _repo_sources in by_root.items():
        files, err = _diff_files(repo, ref) if ref else _status_files(repo)
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


def changed_objects(ws: Workspace, ref: str = "", source: str = "") -> dict:
    """Изменённые объекты рабочей копии: git status (или diff против ref) → объекты.

    ref пуст — незакоммиченные изменения (staged+unstaged+untracked); иначе — против
    ref (ветка/коммит/HEAD~1), плюс текущие untracked-файлы."""
    sources, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    rows, repos_info = _map_changes(ws, sources, ref)
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
    objects = sorted(grouped.values(), key=lambda g: (g["source"], g["object"]))
    return {
        "ref": ref or "(незакоммиченные изменения)",
        "repos": repos_info,
        "object_count": len(objects),
        "objects": objects,
    }


def _touched_ranges(repo: Path, rel: str, ref: str) -> list[tuple[int, int]] | None:
    """New-side line ranges of the file's diff; None когда дифф недоступен (untracked)."""
    args = ["diff", "-U0", ref or "HEAD", "--", rel]
    code, out = _git(args, repo)
    if code != 0:
        return None
    ranges: list[tuple[int, int]] = []
    for line in out.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            ranges.append((start, start + max(count, 1) - 1))
    return ranges


def review_set(ws: Workspace, ref: str = "", max_callers: int = 8, source: str = "") -> dict:
    """Ревью-набор: изменённые строки → затронутые рутины → их вызывающие и override-хуки.

    Для каждого изменённого .bsl: ханки `git diff -U0` мапятся на текущие рутины файла;
    untracked-модули включаются целиком. Каждая затронутая рутина получает callers
    (парсер-верифицировано) и, для экспортных/заимствованных, — override-хуки расширений."""
    sources, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    rows, repos_info = _map_changes(ws, sources, ref)
    changed = changed_objects(ws, ref, source)

    all_overrides = code_intel.find_overrides(ws).get("overrides", [])
    by_target: dict[str, list[dict]] = {}
    for o in all_overrides:
        target = (o.get("target") or "").lower()
        if target:
            by_target.setdefault(target, []).append(o)

    routines: list[dict] = []
    truncated = False
    for src, abs_path, srel, status in rows:
        if not srel.endswith(".bsl"):
            continue
        repo = _repo_root(src.files_root)
        if repo is None:
            continue
        rel_in_repo = str(abs_path.relative_to(repo)).replace("\\", "/")
        ranges = None if status == "??" else _touched_ranges(repo, rel_in_repo, ref)
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
            callers = code_intel.find_callers(
                ws, rt.name, object_hint=hint, max_results=max_callers)
            routines.append({
                "source": src.name,
                "object": descr["object"],
                "module": descr["module"],
                "routine": rt.name,
                "lines": [rt.start_line, rt.end_line],
                "export": rt.export,
                "entry_point": classify_entry_point(rt.name),
                "status": status,
                "callers": callers.get("callers", []),
                "callers_truncated": callers.get("truncated", False),
                "overridden_by": [
                    {k: o.get(k) for k in ("source", "object", "routine", "mode")}
                    for o in by_target.get(rt.name.lower(), [])
                    if (o.get("object") or "").lower() == descr["object"].lower()
                ],
            })
        if truncated:
            break
    return {
        "ref": ref or "(незакоммиченные изменения)",
        "repos": repos_info,
        "changed_objects": changed.get("objects", []),
        "routine_count": len(routines),
        "routines_truncated": truncated,
        "routines": routines,
    }
