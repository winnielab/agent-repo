import asyncio
import sys

import pytest

from repopilot.core.errors import PathBoundaryError
from repopilot.execution.environment import DockerEnvironment, LocalEnvironment


def test_command_success_failure_and_long_output(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path, max_output_chars=20)
    success = asyncio.run(environment.execute(f'{sys.executable} -c "print(123)"'))
    failure = asyncio.run(environment.execute(f'{sys.executable} -c "import sys;sys.exit(7)"'))
    long = asyncio.run(environment.execute(f'{sys.executable} -c "print(\'x\'*100)"'))
    assert success.ok and success.stdout.strip() == "123"
    assert failure.returncode == 7
    assert long.output_truncated
    assert "truncated" in long.stdout
    assert long.stdout.startswith("x")
    assert long.stdout.rstrip().endswith("x")


def test_timeout_and_cwd_escape(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path, default_timeout=0.05)
    result = asyncio.run(environment.execute(f'{sys.executable} -c "import time;time.sleep(1)"'))
    assert result.timed_out and result.returncode == 124
    with pytest.raises(PathBoundaryError):
        asyncio.run(environment.execute("pwd", cwd=tmp_path.parent))


def test_docker_environment_builds_scoped_container_command(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "pkg"
    nested.mkdir(parents=True)
    environment = DockerEnvironment(
        workspace, "example/task:latest", platform="linux/amd64",
        setup_command="source /opt/env.sh",
    )
    args = environment.command_args("python -V", nested)
    assert args[:5] == ["docker", "run", "--rm", "--platform", "linux/amd64"]
    assert f"type=bind,src={workspace},dst=/workspace" in args
    assert args[args.index("-w") + 1] == "/workspace/pkg"
    assert args[-3:] == ["bash", "-lc", "source /opt/env.sh && python -V"]
    with pytest.raises(PathBoundaryError):
        environment.command_args("pwd", tmp_path)


def test_docker_environment_mounts_linked_worktree_git_metadata(tmp_path) -> None:
    source = tmp_path / "source"
    common = source / ".git"
    git_dir = common / "worktrees" / "task"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    environment = DockerEnvironment(workspace, "example/task:latest")
    args = environment.command_args("git status")
    assert f"type=bind,src={common},dst={common}" in args
