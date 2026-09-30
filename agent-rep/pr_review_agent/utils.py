"""Modularized utils component of the PR review agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .settings import MAX_CMD_OUTPUT, MAX_DIFF_CHARS


def now() -> float:
    return time.time()


def truncate(text: str, limit: int = MAX_CMD_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} chars"


def safe_repo_path(repo: Path, path: str) -> Path:
    resolved = (repo / path).resolve()
    repo_resolved = repo.resolve()
    if resolved != repo_resolved and repo_resolved not in resolved.parents:
        raise ValueError(f"Path escapes repository: {path}")
    return resolved


def run_git(repo: Path, args: list[str], timeout: int = 60) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"git {' '.join(args)} failed")
    return output


def is_git_repo(repo: Path) -> bool:
    try:
        run_git(repo, ["rev-parse", "--is-inside-work-tree"], timeout=10)
        return True
    except Exception:
        return False


def load_env_file(path: Path) -> list[str]:
    if not path.exists():
        return []

    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def git_range(repo: Path, base: str, target: str) -> str:
    candidate = f"{base}...{target}"
    try:
        run_git(repo, ["diff", "--quiet", "--exit-code", candidate], timeout=20)
    except RuntimeError as exc:
        msg = str(exc)
        if "no merge base" in msg or "unknown revision" in msg or "bad revision" in msg:
            return f"{base}..{target}"
    return candidate


def iter_review_files(repo: Path, limit: int = 80) -> list[Path]:
    demo_fixture = repo / "demo" / "pr-fixture"
    if demo_fixture.exists():
        return [
            path
            for path in sorted(demo_fixture.rglob("*"))
            if path.is_file() and is_source_file(path.relative_to(repo).as_posix())
        ][:limit]

    excluded = {
        ".git",
        ".conda",
        ".next",
        ".pytest_cache",
        ".review-agent",
        ".tmp",
        "node_modules",
        "__pycache__",
    }
    files: list[Path] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if any(part in excluded for part in rel.parts):
            continue
        if is_source_file(str(rel)) or str(rel).replace("\\", "/").startswith("skills/"):
            files.append(path)
        if len(files) >= limit:
            break
    return files


def synthetic_changed_files(repo: Path) -> list[dict[str, Any]]:
    files = []
    for path in iter_review_files(repo):
        rel = path.relative_to(repo).as_posix()
        try:
            added = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except Exception:
            added = 0
        files.append({"file": rel, "added": added, "deleted": 0})
    return files


def synthetic_diff(repo: Path) -> str:
    chunks: list[str] = []
    for path in iter_review_files(repo, limit=40):
        rel = path.relative_to(repo).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        chunks.extend(
            [
                f"diff --git a/{rel} b/{rel}",
                "--- /dev/null",
                f"+++ b/{rel}",
                f"@@ -0,0 +1,{len(lines)} @@",
            ]
        )
        chunks.extend("+" + line for line in lines[:300])
    return truncate("\n".join(chunks), MAX_DIFF_CHARS)


def parse_numstat(output: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for raw in output.splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, file_path = parts[0], parts[1], parts[2]
        files.append(
            {
                "file": file_path,
                "added": None if added == "-" else int(added),
                "deleted": None if deleted == "-" else int(deleted),
            }
        )
    return files


def iter_added_lines(diff: str):
    current_file = ""
    new_line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            continue
        if raw.startswith("+++ /dev/null"):
            current_file = ""
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            new_line = int(match.group(1)) if match else 0
            continue
        if not current_file or not raw:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            yield {"file": current_file, "line": new_line, "text": raw[1:]}
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1


def is_test_file(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        "/test/" in normalized
        or "/tests/" in normalized
        or normalized.startswith("test/")
        or normalized.startswith("tests/")
        or name.startswith("test_")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.tsx")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
    )


def is_source_file(path: str) -> bool:
    return Path(path).suffix.lower() in {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".cs",
        ".rb",
        ".php",
    }
