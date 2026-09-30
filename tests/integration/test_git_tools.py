import asyncio
import subprocess

from repopilot.core.models import ToolCall
from repopilot.execution.environment import LocalEnvironment
from repopilot.observability.events import EventBus
from repopilot.tools.base import ToolContext
from repopilot.tools.git import GitDiffTool
from repopilot.tools.registry import ToolRegistry


def test_git_diff_is_scoped_to_workspace(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("before\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("after\n")
    context = ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", EventBus("run"))
    result = asyncio.run(ToolRegistry([GitDiffTool()]).execute(ToolCall("1", "git_diff", {}), context))
    assert result.ok
    assert "+after" in result.content and "-before" in result.content
