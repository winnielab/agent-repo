"""Task-bound Git worktrees and durable patch artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from repopilot.execution.git import run_git
from repopilot.execution.workspace import Workspace, WorkspaceManager
from repopilot.task.graph import TaskGraph, TaskRecord


@dataclass
class TaskWorkspaceLease:
    task_id: str
    attempt: int
    owner: str
    workspace_id: str
    base_commit: str
    workspace: Workspace
    artifact_dir: Path

    def persist_patch(self) -> tuple[Path, str]:
        patch = self.workspace.collect_diff()
        if not patch.strip():
            raise ValueError(f"task {self.task_id} produced an empty patch")
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"{self.task_id}.attempt-{self.attempt}.patch"
        temporary = path.with_suffix(".patch.tmp")
        temporary.write_text(patch, encoding="utf-8")
        temporary.replace(path)
        digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        return path, digest


class TaskWorkspaceProvider:
    """Create one exclusive linked checkout for every claimed TaskRecord."""

    def __init__(self, source_repo: Path, graph: TaskGraph, run_id: str, parent: Path,
                 artifact_dir: Path, workspace_manager: Optional[WorkspaceManager] = None) -> None:
        if not run_id.strip():
            raise ValueError("run_id is required")
        self.source_repo = Path(source_repo).resolve()
        self.graph = graph
        self.run_id = run_id
        self.parent = Path(parent).resolve()
        self.artifact_dir = Path(artifact_dir)
        self.manager = workspace_manager or WorkspaceManager(self.parent)

    def acquire(self, record: TaskRecord, owner: str, *, base_ref: str = "HEAD",
                agent_session_id: Optional[str] = None) -> TaskWorkspaceLease:
        current = self.graph.get(record.id)
        if current.lease_owner != owner:
            raise ValueError(f"task lease is not owned by {owner}: {record.id}")
        base_commit = run_git(self.source_repo, ["rev-parse", base_ref]).strip()
        workspace_id = f"{self.run_id}-{record.id}-attempt-{current.attempt}"
        workspace = self.manager.create(self.source_repo, workspace_id, base_commit)
        try:
            self.graph.bind_workspace(
                record.id, owner, workspace_id, base_commit,
                agent_session_id=agent_session_id,
            )
        except Exception:
            workspace.cleanup(force=True)
            raise
        return TaskWorkspaceLease(
            record.id, current.attempt, owner, workspace_id, base_commit, workspace,
            self.artifact_dir,
        )
