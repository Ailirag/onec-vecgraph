"""Text search over a lite workspace: ripgrep streaming with a pure-Python fallback.

ripgrep is optional. Discovery order: env ONEC_LITE_RG -> PATH -> the WinGet links dir.
All searches are scoped to kind folders of the workspace's sources, so stray files in
the repository (docs, DT-INF, .git) never pollute results.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from ..parsing.dump import TYPE_FOLDERS
from .workspace import LiteSource, Workspace, read_text

_MAX_FILE_BYTES = 4_000_000  # python fallback: skip pathological files


_RG_OVERRIDE: str | None = None  # explicit path (admin form / saved state) beats discovery


def set_rg_path(path: str | None) -> str | None:
    """Set/clear the explicit ripgrep path; returns the now-effective rg_path()."""
    global _RG_OVERRIDE
    _RG_OVERRIDE = (path or "").strip().strip('"') or None
    _discover_rg.cache_clear()
    return rg_path()


def rg_override() -> str | None:
    return _RG_OVERRIDE


def rg_path() -> str | None:
    if _RG_OVERRIDE:
        return _RG_OVERRIDE if Path(_RG_OVERRIDE).is_file() else None
    return _discover_rg()


@lru_cache(maxsize=1)
def _discover_rg() -> str | None:
    env = os.environ.get("ONEC_LITE_RG", "").strip()
    if env:
        return env if Path(env).is_file() else None
    found = shutil.which("rg")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    links = local / "Microsoft" / "WinGet" / "Links" / "rg.exe"
    if links.is_file():
        return str(links)
    # VS Code bundles ripgrep; the install dir may contain a versioned hash folder.
    vscode = local / "Programs" / "Microsoft VS Code"
    if vscode.is_dir():
        for pattern in (
            "resources/app/node_modules/@vscode/ripgrep*/bin/**/rg.exe",
            "*/resources/app/node_modules/@vscode/ripgrep*/bin/**/rg.exe",
        ):
            for hit in vscode.glob(pattern):
                return str(hit)
    return None


def _search_dirs(sources: list[LiteSource], kinds: set[str] | None) -> list[str]:
    folders = (
        sorted(TYPE_FOLDERS)
        if kinds is None
        else sorted(f for f, k in TYPE_FOLDERS.items() if k in kinds)
    )
    dirs: list[str] = []
    for s in sources:
        for folder in folders:
            d = s.files_root / folder
            if d.is_dir():
                dirs.append(str(d))
    return dirs


def rg_stream(
    pattern: str,
    dirs: list[str],
    *,
    glob: str = "*.bsl",
    literal: bool = False,
    case_sensitive: bool = False,
    max_hits: int = 100,
) -> Iterator[tuple[str, int, str]]:
    """Yield (abs_path, line_no, line_text) from ripgrep --json, stopping at max_hits.

    Raises FileNotFoundError when ripgrep is not available (callers fall back to Python).
    """
    rg = rg_path()
    if not rg:
        raise FileNotFoundError("ripgrep не найден")
    if not dirs:
        return
    args = [rg, "--json", "--glob", glob, "--no-messages"]
    args += ["-s"] if case_sensitive else ["-i"]
    if literal:
        args += ["-F"]
    args += ["-e", pattern, "--", *dirs]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    count = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "match":
                continue
            data = obj["data"]
            yield data["path"]["text"], data["line_number"], data["lines"]["text"].rstrip("\n")
            count += 1
            if count >= max_hits:
                break
    finally:
        proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()


def _python_stream(
    rx: re.Pattern[str], files: list[Path], max_hits: int
) -> Iterator[tuple[str, int, str]]:
    count = 0
    for path in files:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = read_text(path)
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                yield str(path), i, line
                count += 1
                if count >= max_hits:
                    return


def stream(
    ws: Workspace,
    pattern: str,
    *,
    sources: list[LiteSource],
    kinds: set[str] | None = None,
    glob: str = "*.bsl",
    regex: bool = True,
    case_sensitive: bool = False,
    max_hits: int = 100,
) -> tuple[str, Iterator[tuple[str, int, str]]]:
    """(engine, iterator of (abs_path, line, text)) — ripgrep when present, Python otherwise."""
    dirs = _search_dirs(sources, kinds)
    # rg_stream is a generator: its FileNotFoundError would only surface on first next(),
    # past any try/except here — so probe availability eagerly instead.
    if rg_path():
        return "ripgrep", rg_stream(
            pattern, dirs, glob=glob, literal=not regex,
            case_sensitive=case_sensitive, max_hits=max_hits,
        )
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(pattern if regex else re.escape(pattern), flags)
    files: list[Path] = []
    if glob == "*.bsl":
        for s in sources:
            files.extend(ws.bsl_files(s, kinds))
    else:
        suffix = glob.lstrip("*")
        for d in dirs:
            for walk_root, _dirs, names in os.walk(d):
                files.extend(Path(walk_root) / n for n in names if n.endswith(suffix))
    return "python", _python_stream(rx, files, max_hits)


def count_total(
    pattern: str, dirs: list[str], *, glob: str = "*.bsl", case_sensitive: bool = False
) -> int | None:
    """Total regex matches across dirs via `rg --count-matches`; None when rg is absent."""
    rg = rg_path()
    if not rg:
        return None
    if not dirs:
        return 0
    args = [rg, "--count-matches", "--glob", glob, "--no-messages"]
    args += ["-s"] if case_sensitive else ["-i"]
    args += ["-e", pattern, "--", *dirs]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    total = 0
    for line in proc.stdout.splitlines():
        # Windows paths contain ':', so split from the right: <path>:<count>
        _p, _sep, cnt = line.rpartition(":")
        if cnt.isdigit():
            total += int(cnt)
    return total


def search_code(
    ws: Workspace,
    pattern: str,
    *,
    kinds: set[str] | None = None,
    name_filter: str = "",
    regex: bool = True,
    case_sensitive: bool = False,
    max_results: int = 100,
    source: str = "",
) -> dict:
    """Full-text search over .bsl modules; rows carry source + source-relative path."""
    srcs, err = ws.resolve_sources(source)
    if err:
        return {"error": err}
    nf = name_filter.lower()
    # With a post-filter we must overfetch: matches outside the wanted object are dropped.
    cap = max_results if not nf else max(max_results * 100, 5000)
    engine, it = stream(
        ws, pattern, sources=srcs, kinds=kinds, regex=regex,
        case_sensitive=case_sensitive, max_hits=cap,
    )
    rows: list[dict] = []
    truncated = False
    seen_stream = 0
    for abs_path, line_no, text in it:
        seen_stream += 1
        src_name, rel = ws.source_of_path(Path(abs_path))
        if nf:
            parts = rel.split("/")
            if len(parts) < 2 or nf not in parts[1].lower():
                continue
        rows.append({"source": src_name, "path": rel, "line": line_no, "text": text.strip()[:300]})
        if len(rows) >= max_results:
            truncated = True
            break
    if not truncated and nf and seen_stream >= cap:
        truncated = True
    return {
        "pattern": pattern,
        "engine": engine,
        "match_count": len(rows),
        "truncated": truncated,
        "matches": rows,
    }
