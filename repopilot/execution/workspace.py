"""Isolated Git-worktree lifecycle for solve runs."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from repopilot.core.errors import WorkspaceError
from repopilot.execution.git import is_git_repo, run_git
from repopilot.observability.events import EventBus


RUNTIME_ARTIFACT_SUFFIXES = frozenset({".so", ".pyd", ".dylib", ".dll"})


def _normalize_linked_submodules(worktree: Path) -> None:
    """Repair submodule metadata emitted incorrectly for an external worktree.

    Git can write a relative ``gitdir`` beginning with ``../../<absolute path>``
    when a populated repository is copied out of a Linux container and its new
    linked worktree lives elsewhere on macOS.  Root Git operations then fail
    while inspecting the submodule.  Point each initialized submodule at the
    metadata Git created for this linked worktree and make its worktree path
    explicit.
    """
    git_dir = Path(run_git(worktree, ["rev-parse", "--absolute-git-dir"]).strip())
    modules_dir = git_dir / "modules"
    if not modules_dir.is_dir():
        return
    for marker in worktree.rglob(".git"):
        if marker == worktree / ".git" or not marker.is_file():
            continue
        relative = marker.parent.relative_to(worktree)
        candidate = modules_dir / relative
        if not candidate.is_dir():
            matches = [
                path.parent
                for path in modules_dir.rglob("config")
                if path.parent.name == relative.name and (path.parent / "HEAD").is_file()
            ]
            if len(matches) != 1:
                continue
            candidate = matches[0]
        marker.write_text(f"gitdir: {candidate}\n", encoding="utf-8")
        run_git(
            worktree,
            ["config", "--file", str(candidate / "config"), "core.worktree", str(marker.parent)],
        )


def _hydrate_ignored_runtime_artifacts(
    source: Path,
    worktree: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[int, int]:
    """Copy only ignored native runtime artifacts into a linked worktree.

    Git worktrees contain tracked files only.  Repositories exported from a
    prepared build image can also need ignored extension modules at runtime;
    mounting the linked worktree over the image then hides those modules.  The
    allowlist and size bounds deliberately exclude arbitrary untracked files,
    including tests and generated source.
    """
    ignored = run_git(
        source,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    )
    candidates = []
    total_bytes = 0
    for relative_text in filter(None, ignored.split("\0")):
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceError(f"unsafe ignored artifact path: {relative_text}")
        if relative.suffix.lower() not in RUNTIME_ARTIFACT_SUFFIXES:
            continue
        source_path = source / relative
        if source_path.is_symlink() or not source_path.is_file():
            continue
        resolved_source = source_path.resolve()
        if not resolved_source.is_relative_to(source):
            raise WorkspaceError(f"ignored artifact escapes source repository: {relative_text}")
        size = source_path.stat().st_size
        candidates.append((relative, source_path, size))
        total_bytes += size
        if len(candidates) > max_files:
            raise WorkspaceError(
                f"ignored runtime artifact count exceeds limit ({max_files})"
            )
        if total_bytes > max_bytes:
            raise WorkspaceError(
                f"ignored runtime artifacts exceed byte limit ({max_bytes})"
            )

    for relative, source_path, _size in candidates:
        destination = worktree / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
    return len(candidates), total_bytes


@dataclass
class Workspace:
    source_repo: Path
    path: Path
    run_id: str
    base_ref: str
    event_bus: EventBus
    _diff_collected: bool = field(default=False, init=False)

    def collect_diff(self) -> str:
        tracked = run_git(self.path, ["diff", "--binary", "--no-ext-diff", "HEAD"])
        chunks = [tracked.rstrip("\n")] if tracked else []
        untracked = run_git(self.path, ["ls-files", "--others", "--exclude-standard", "-z"])
        for relative in filter(None, untracked.split("\0")):
            # `git diff --no-index` exits 1 when differences exist, so build the
            # patch through a temporary index-free subprocess below.
            completed = subprocess.run(
                ["git", "diff", "--binary", "--no-index", "--", "/dev/null", relative],
                cwd=str(self.path), capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if completed.returncode not in (0, 1):
                raise WorkspaceError(completed.stderr.strip() or "could not diff untracked file")
            chunks.append(completed.stdout.rstrip("\n"))
        self._diff_collected = True
        return "\n".join(chunk for chunk in chunks if chunk) + ("\n" if chunks else "")

    def changed_files(self) -> List[str]:
        output = run_git(self.path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        names: List[str] = []
        parts = output.split("\0")
        index = 0
        while index < len(parts):
            entry = parts[index]
            index += 1
            if not entry:
                continue
            status, name = entry[:2], entry[3:]
            if status[0] in {"R", "C"}:
                if index < len(parts) and parts[index]:
                    name = parts[index]
                    index += 1
            names.append(name)
        return sorted(set(names))

    def cleanup(self, force: bool = False) -> None:
        changes = self.changed_files() if self.path.exists() else []
        if changes and not self._diff_collected and not force:
            raise WorkspaceError("workspace has uncollected changes; collect_diff before cleanup")
        self.event_bus.emit("workspace_cleanup_started", workspace=str(self.path), changed_files=changes)
        run_git(self.source_repo, ["worktree", "remove", "--force", str(self.path)])
        run_git(self.source_repo, ["worktree", "prune"])
        self.event_bus.emit("workspace_cleanup_finished", workspace=str(self.path))


class WorkspaceManager:
    def __init__(
        self,
        parent: Optional[Path] = None,
        event_bus: Optional[EventBus] = None,
        *,
        hydrate_runtime_artifacts: bool = True,
        max_runtime_artifact_files: int = 4096,
        max_runtime_artifact_bytes: int = 1_073_741_824,
    ) -> None:
        self.parent = Path(parent).resolve() if parent else None
        self.event_bus = event_bus
        self.hydrate_runtime_artifacts = hydrate_runtime_artifacts
        self.max_runtime_artifact_files = max_runtime_artifact_files
        self.max_runtime_artifact_bytes = max_runtime_artifact_bytes

    @staticmethod
    def _safe_name(run_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id).strip("-.")[:80]
        if not sanitized:
            raise WorkspaceError("run ID does not contain a safe worktree name")
        return "repopilot-" + sanitized

    def create(self, repo: Path, run_id: str, base_ref: str = "HEAD") -> Workspace:
        source = Path(repo).resolve()
        if not is_git_repo(source):
            raise WorkspaceError(f"not a Git repository: {source}")
        parent = self.parent or source.parent / ".repopilot-worktrees"
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / self._safe_name(run_id)
        if path.exists():
            raise WorkspaceError(f"worktree path already exists: {path}")
        bus = self.event_bus or EventBus(run_id)
        try:
            run_git(source, ["worktree", "add", "--detach", str(path), base_ref])
            _normalize_linked_submodules(path)
            if self.hydrate_runtime_artifacts:
                artifact_count, artifact_bytes = _hydrate_ignored_runtime_artifacts(
                    source,
                    path,
                    max_files=self.max_runtime_artifact_files,
                    max_bytes=self.max_runtime_artifact_bytes,
                )
                bus.emit(
                    "workspace_runtime_artifacts_hydrated",
                    workspace=str(path),
                    artifact_count=artifact_count,
                    artifact_bytes=artifact_bytes,
                    suffixes=sorted(RUNTIME_ARTIFACT_SUFFIXES),
                )
            # Fail at workspace creation rather than after an agent has spent
            # its budget if Git still cannot traverse linked submodules.
            run_git(path, ["status", "--porcelain=v1", "--untracked-files=no"])
        except Exception:
            if path.exists():
                try:
                    run_git(source, ["worktree", "remove", "--force", str(path)])
                except WorkspaceError:
                    shutil.rmtree(path, ignore_errors=True)
            try:
                run_git(source, ["worktree", "prune"])
            except WorkspaceError:
                pass
            raise
        workspace = Workspace(source, path, run_id, base_ref, bus)
        bus.emit(
            "workspace_created",
            source_repo=str(source),
            workspace=str(path),
            base_ref=base_ref,
        )
        return workspace
