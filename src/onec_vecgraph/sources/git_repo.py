"""Materialize a source's content directory: a local path, or a shallow git clone/pull.

Uses the system `git` (no Python git dependency). Clones are cached under <cache>/<sha-of-repo>.
Tests pass a local `path` to avoid any network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import sha1_text


def materialize(entry: dict, cache_root: str = ".cache/sources") -> Path:
    """Return a local directory for the source.

    entry: {path: <local dir>} OR {repo: <git url>, branch?: <ref>}. `path` wins (offline/testing).
    """
    if entry.get("path"):
        p = Path(entry["path"])
        if not p.is_dir():
            raise NotADirectoryError(f"source path is not a directory: {p}")
        return p
    repo = entry.get("repo")
    if not repo:
        raise ValueError("source entry needs either 'path' or 'repo'")
    branch = entry.get("branch")
    dest = Path(cache_root) / sha1_text(repo, branch or "")[:16]
    if (dest / ".git").is_dir():
        cmd = ["git", "-C", str(dest), "pull", "--ff-only", "--depth", "1"]
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [repo, str(dest)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return dest


def iter_files(root: Path, globs: list[str]) -> list[Path]:
    """All files under root matching any glob, sorted, de-duplicated, excluding .git/."""
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in globs:
        for f in sorted(root.glob(pattern)):
            if f.is_file() and ".git" not in f.parts and f not in seen:
                seen.add(f)
                out.append(f)
    return out
