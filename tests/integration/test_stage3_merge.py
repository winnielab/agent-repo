import asyncio
import subprocess

from repopilot.execution.git import run_git
from repopilot.execution.workspace import WorkspaceManager
from repopilot.orchestration.merge_manager import MergeManager, PatchArtifact
from repopilot.orchestration.workspace import TaskWorkspaceProvider
from repopilot.task.graph import TaskGraph


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "a.txt").write_text("a0\n")
    (path / "b.txt").write_text("b0\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")


def patch_for(repo, worktrees, name, filename, value, patches):
    workspace = WorkspaceManager(worktrees).create(repo, name)
    (workspace.path / filename).write_text(value)
    patch = workspace.collect_diff()
    path = patches / f"{name}.patch"
    path.write_text(patch)
    return workspace, PatchArtifact.from_path(name, path, run_git(repo, ["rev-parse", "HEAD"]).strip())


def test_merge_manager_applies_disjoint_patches_then_verifies(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    patches = tmp_path / "patches"
    patches.mkdir()
    first, first_patch = patch_for(repo, tmp_path / "workers", "one", "a.txt", "a1\n", patches)
    second, second_patch = patch_for(repo, tmp_path / "workers", "two", "b.txt", "b1\n", patches)
    target = WorkspaceManager(tmp_path / "merge").create(repo, "target")

    async def verify(path):
        return {"ok": (path / "a.txt").read_text() == "a1\n" and (path / "b.txt").read_text() == "b1\n"}

    result = asyncio.run(MergeManager(target.path, verifier=verify).merge([
        first_patch, second_patch,
    ]))

    assert result.success
    assert result.classification == "disjoint"
    assert result.changed_files == ["a.txt", "b.txt"]
    for workspace in (first, second, target):
        workspace.collect_diff()
        workspace.cleanup()


def test_merge_manager_rejects_overlapping_same_file_hunks_without_mutation(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    patches = tmp_path / "patches"
    patches.mkdir()
    first, first_patch = patch_for(repo, tmp_path / "workers", "one", "a.txt", "a1\n", patches)
    second, second_patch = patch_for(repo, tmp_path / "workers", "two", "a.txt", "a2\n", patches)
    target = WorkspaceManager(tmp_path / "merge").create(repo, "target")

    result = asyncio.run(MergeManager(target.path).merge([first_patch, second_patch]))

    assert result.status == "conflict"
    assert result.classification == "same_file_conflict"
    assert (target.path / "a.txt").read_text() == "a0\n"
    for workspace in (first, second, target):
        workspace.collect_diff()
        workspace.cleanup()


def test_task_workspace_provider_binds_exact_base_and_persists_patch(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    graph = TaskGraph(tmp_path / "tasks")
    task = graph.add("A", "change a")
    claimed = graph.claim_ready("run:worker")
    assert claimed is not None
    provider = TaskWorkspaceProvider(
        repo, graph, "run-1", tmp_path / "workers", tmp_path / "artifacts",
    )

    lease = provider.acquire(claimed, "run:worker")
    (lease.workspace.path / "a.txt").write_text("changed\n")
    path, digest = lease.persist_patch()

    persisted = TaskGraph(tmp_path / "tasks").get(task.id)
    assert persisted.workspace_id == lease.workspace_id
    assert persisted.base_commit == run_git(repo, ["rev-parse", "HEAD"]).strip()
    assert path.is_file()
    assert len(digest) == 64
    lease.workspace.cleanup()
