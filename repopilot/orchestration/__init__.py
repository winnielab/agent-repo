"""Stage 3 task coordination, isolated workspaces, and merge primitives."""

from .coordinator import AgentCoordinator, CoordinatorResult, WorkerAssignment
from .merge_manager import MergeManager, MergeResult, PatchArtifact
from .workspace import TaskWorkspaceLease, TaskWorkspaceProvider

__all__ = [
    "AgentCoordinator",
    "CoordinatorResult",
    "MergeManager",
    "MergeResult",
    "PatchArtifact",
    "TaskWorkspaceLease",
    "TaskWorkspaceProvider",
    "WorkerAssignment",
]
