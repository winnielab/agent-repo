import subprocess
from pathlib import Path

import pytest

from repopilot.core.errors import WorkspaceError
from repopilot.execution.git import run_git
from repopilot.execution.workspace import WorkspaceManager, _normalize_linked_submodules


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "value.txt").write_text("before\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")


def test_worktree_isolated_diff_and_cleanup(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    workspace = WorkspaceManager(tmp_path / "worktrees").create(repo, "run-1")
    (workspace.path / "value.txt").write_text("after\n")
    assert (repo / "value.txt").read_text() == "before\n"
    assert "after" in workspace.collect_diff()
    assert workspace.changed_files() == ["value.txt"]
    workspace.cleanup()
    assert not workspace.path.exists()
    assert str(workspace.path) not in run_git(repo, ["worktree", "list", "--porcelain"])


def test_cleanup_refuses_uncollected_changes(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    workspace = WorkspaceManager(tmp_path / "worktrees").create(repo, "run-2")
    (workspace.path / "value.txt").write_text("changed\n")
    with pytest.raises(WorkspaceError):
        workspace.cleanup()
    workspace.collect_diff()
    workspace.cleanup()


def test_normalizes_broken_linked_submodule_metadata(tmp_path) -> None:
    child = tmp_path / "child"
    make_repo(child)
    repo = tmp_path / "repo"
    make_repo(repo)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(child), "vendor/child"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    git(repo, "commit", "-qam", "add child")
    workspace = WorkspaceManager(tmp_path / "worktrees").create(repo, "with-submodule")
    marker = workspace.path / "vendor" / "child" / ".git"
    git_dir = Path(run_git(workspace.path, ["rev-parse", "--absolute-git-dir"]).strip())
    candidate = git_dir / "modules" / "vendor" / "child"
    candidate.mkdir(parents=True)
    (candidate / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (candidate / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    marker.write_text("gitdir: ../../Users/example/broken/modules/vendor/child\n", encoding="utf-8")
    _normalize_linked_submodules(workspace.path)
    target = Path(marker.read_text(encoding="utf-8").removeprefix("gitdir: ").strip())
    assert target.is_absolute()
    assert target.is_dir()
    marker.unlink()
    workspace.cleanup()


def test_hydrates_only_ignored_native_runtime_artifacts(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text("*.so\n*.secret\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore generated artifacts")
    native = repo / "package" / "_native.cpython-39-x86_64-linux-gnu.so"
    native.parent.mkdir()
    native.write_bytes(b"compiled-extension")
    ignored_non_runtime = repo / "package" / "hidden.secret"
    ignored_non_runtime.write_text("do not copy", encoding="utf-8")

    workspace = WorkspaceManager(tmp_path / "worktrees").create(repo, "native-runtime")

    copied = workspace.path / native.relative_to(repo)
    assert copied.read_bytes() == b"compiled-extension"
    assert not (workspace.path / ignored_non_runtime.relative_to(repo)).exists()
    assert workspace.changed_files() == []
    assert workspace.collect_diff() == ""
    workspace.cleanup()


def test_runtime_artifact_hydration_can_be_disabled(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text("*.so\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore native extension")
    native = repo / "package" / "_native.so"
    native.parent.mkdir()
    native.write_bytes(b"compiled-extension")

    workspace = WorkspaceManager(
        tmp_path / "worktrees",
        hydrate_runtime_artifacts=False,
    ).create(repo, "native-runtime-disabled")

    assert not (workspace.path / native.relative_to(repo)).exists()
    workspace.cleanup()


def test_runtime_artifact_hydration_fails_closed_on_size_limit(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text("*.so\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore native extension")
    native = repo / "package" / "_native.so"
    native.parent.mkdir()
    native.write_bytes(b"too-large")
    manager = WorkspaceManager(
        tmp_path / "worktrees",
        max_runtime_artifact_bytes=4,
    )

    with pytest.raises(WorkspaceError, match="byte limit"):
        manager.create(repo, "native-runtime-too-large")

    failed_path = tmp_path / "worktrees" / "repopilot-native-runtime-too-large"
    assert not failed_path.exists()
    assert str(failed_path) not in run_git(repo, ["worktree", "list", "--porcelain"])
