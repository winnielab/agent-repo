import asyncio
import json
import subprocess

from repopilot.config import RepoPilotConfig
from repopilot.core.models import LLMResponse, ToolCall, Usage
from repopilot.llm.scripted import ScriptedLLM
from repopilot.workflows.solve_task import solve_task


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_calculator_end_to_end_in_isolated_worktree(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "calculator.py").write_text("def divide(a, b):\n    return a * b\n")
    (repo / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import divide\n"
        "class T(unittest.TestCase):\n    def test_divide(self): self.assertEqual(divide(8, 2), 4)\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    original = (repo / "calculator.py").read_text()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("read", "read_file", {"path": "calculator.py"})], usage=Usage(10, 2)),
        LLMResponse(tool_calls=[ToolCall("edit", "edit_file", {
            "path": "calculator.py", "old_text": "return a * b", "new_text": "return a / b",
        })], usage=Usage(12, 3)),
        LLMResponse(tool_calls=[
            ToolCall("test", "run_tests", {}), ToolCall("diff", "git_diff", {}),
        ], usage=Usage(15, 4)),
        LLMResponse(text="Fixed divide and verified tests.", usage=Usage(10, 5), stop_reason="stop"),
    ])
    result = asyncio.run(solve_task(
        repo, "Fix divide() so all tests pass", llm,
        RepoPilotConfig(test_command="python -m unittest discover -v",
                        worktree_parent=tmp_path / "worktrees", keep_worktree=True),
        run_id="calculator-e2e",
    ))
    assert result.agent.success
    assert result.tests["ok"] is True
    assert "return a / b" in result.diff
    assert result.agent.changed_files == ["calculator.py"]
    assert (repo / "calculator.py").read_text() == original
    events = [json.loads(line) for line in result.event_log.read_text().splitlines()]
    event_types = {event["type"] for event in events}
    assert {"llm_called", "tool_called", "test_finished", "file_changed", "run_finished"} <= event_types
    report = json.loads(result.report_path.read_text())
    assert report["iterations"] == result.agent.iterations == 4
