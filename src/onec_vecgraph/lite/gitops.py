"""Configurable workspace updates from remote: fetch, guarded ff-pull, managed mirrors.

Two workspace kinds get different write rules:
  * path workspaces point at somebody's working copy — `fetch` is always safe (does not
    touch the tree); `pull` runs ONLY explicitly and only with guards: it must be a git
    repo, the tree must be clean, HEAD must be on a branch, and the merge must be
    fast-forward. We never risk a developer's uncommitted work.
  * mirror workspaces ({repo, branch?}) are clones OWNED by lite under
    ~/.onec-lite/mirrors/<name> — full clones (history is needed by gitview's
    changed_objects/review_set), so updating them is always safe.

All git runs are non-interactive (GIT_TERMINAL_PROMPT=0): a missing credential fails
fast with a readable error instead of hanging the server. Update triggers live in the
admin page / CLI (`onec-lite update`) / per-workspace `update_on_start` — deliberately
NOT as an MCP tool: the MCP surface stays read-only, exactly like the big server.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import admin as lite_admin

UPDATE_MODES = ("off", "fetch", "pull")
_TIMEOUT = 600  # clone/pull of a large config repo over corp VPN can be slow


def run_git(args: list[str], cwd: Path | None, timeout: int = _TIMEOUT) -> tuple[int, str]:
    """System git, non-interactive, utf-8; (rc, output). rc=127 when git is missing."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # no hidden credential prompts under a server
    env.setdefault("GCM_INTERACTIVE", "Never")
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "git не найден в PATH"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = proc.stdout if proc.returncode == 0 else ((proc.stderr or "") + (proc.stdout or ""))
    return proc.returncode, out.strip()


def is_git_repo(root: Path) -> bool:
    code, out = run_git(["rev-parse", "--is-inside-work-tree"], root, timeout=30)
    return code == 0 and out.strip() == "true"


def status_brief(root: Path) -> dict:
    """Cheap git view for the admin table: branch, dirty flag, ahead/behind vs upstream."""
    if not root.is_dir() or not is_git_repo(root):
        return {"git": False}
    _c, branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], root, timeout=30)
    code, porcelain = run_git(["status", "--porcelain"], root, timeout=60)
    dirty = bool(porcelain.strip()) if code == 0 else None
    ahead = behind = None
    code, counts = run_git(
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], root, timeout=30)
    if code == 0 and counts:
        parts = counts.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    return {"git": True, "branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind}


def fetch(root: Path) -> dict:
    """Safe for any working copy: updates origin/* refs only, never the tree."""
    if not is_git_repo(root):
        return {"ok": False, "op": "fetch", "error": f"не git-репозиторий: {root}"}
    code, out = run_git(["fetch", "--prune"], root)
    if code != 0:
        return {"ok": False, "op": "fetch", "error": out or f"git fetch: код {code}"}
    return {"ok": True, "op": "fetch", "output": out or "уже актуально",
            **status_brief(root)}


def pull_ff(root: Path) -> dict:
    """Guarded update of a path workspace: clean tree + on-branch + fast-forward only."""
    if not is_git_repo(root):
        return {"ok": False, "op": "pull", "error": f"не git-репозиторий: {root}"}
    code, porcelain = run_git(["status", "--porcelain"], root, timeout=60)
    if code != 0:
        return {"ok": False, "op": "pull", "error": porcelain}
    if porcelain.strip():
        n = len(porcelain.strip().splitlines())
        return {"ok": False, "op": "pull",
                "error": f"рабочее дерево не чистое ({n} изменённых файлов) — pull отменён, "
                         "чтобы не смешаться с локальной работой; сделайте fetch или commit/stash"}
    code, branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], root, timeout=30)
    if code != 0 or branch.strip() == "HEAD":
        return {"ok": False, "op": "pull",
                "error": "HEAD не на ветке (detached) — pull отменён"}
    code, out = run_git(["pull", "--ff-only"], root)
    if code != 0:
        return {"ok": False, "op": "pull",
                "error": out or f"git pull --ff-only: код {code} (расхождение истории?)"}
    return {"ok": True, "op": "pull", "output": out, **status_brief(root)}


# --------------------------------------------------------------------------- #
# Mirrors: clones owned by lite (~/.onec-lite/mirrors/<name>)
# --------------------------------------------------------------------------- #

def mirrors_root() -> Path:
    return lite_admin.state_file().parent / "mirrors"


def mirror_path(name: str) -> Path:
    return mirrors_root() / name


def update_mirror(name: str, repo: str, branch: str = "") -> dict:
    """Clone (first time, FULL history — gitview needs it) or ff-pull an owned mirror."""
    dest = mirror_path(name)
    if (dest / ".git").is_dir():
        res = pull_ff(dest)
        res["op"] = "mirror-pull"
        res["root"] = str(dest)
        return res
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["clone"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo, str(dest)]
    code, out = run_git(cmd, None)
    if code != 0:
        return {"ok": False, "op": "mirror-clone", "error": out or f"git clone: код {code}"}
    return {"ok": True, "op": "mirror-clone", "root": str(dest),
            "output": out or "клонировано", **status_brief(dest)}


def update_workspace(name: str, entry: dict, mode: str = "") -> dict:
    """Dispatcher used by the admin button / CLI / update_on_start.

    mode: ''/'auto' -> зеркало обновляется pull-ом, путь — безопасным fetch;
    явные 'fetch' | 'pull' форсируют операцию (для пути pull остаётся с предохранителями)."""
    repo = str(entry.get("repo") or "").strip()
    if repo:
        if mode == "fetch" and (mirror_path(name) / ".git").is_dir():
            return {**fetch(mirror_path(name)), "root": str(mirror_path(name))}
        return update_mirror(name, repo, str(entry.get("branch") or "").strip())
    root = Path(str(entry.get("root") or ""))
    if mode == "pull":
        return pull_ff(root)
    return fetch(root)
