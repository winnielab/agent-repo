"""Small, argument-safe Git command helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from repopilot.core.errors import WorkspaceError


def run_git(repo: Path, args: Iterable[str], timeout: float = 30.0) -> str:
    arguments = ["git", "-c", "core.hooksPath=/dev/null", *list(args)]
    try:
        result = subprocess.run(
            arguments,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"git command failed to run: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorkspaceError(detail or f"git {' '.join(arguments[1:])} failed")
    return result.stdout


def is_git_repo(repo: Path) -> bool:
    try:
        return run_git(repo, ["rev-parse", "--is-inside-work-tree"]).strip() == "true"
    except WorkspaceError:
        return False

