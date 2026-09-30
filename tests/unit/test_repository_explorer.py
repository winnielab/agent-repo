import asyncio

from repopilot.core.models import LLMResponse, ToolCall
from repopilot.core.runtime import AgentRuntime
from repopilot.execution.environment import LocalEnvironment
from repopilot.execution.permission import PermissionManager
from repopilot.llm.scripted import ScriptedLLM
from repopilot.observability.events import EventBus
from repopilot.repo.explorer import RepositoryExplorer
from repopilot.tools.base import ToolContext
from repopilot.tools.editing import WriteFileTool
from repopilot.tools.filesystem import GlobTool, ReadFileTool
from repopilot.tools.git import GitStatusTool
from repopilot.tools.registry import ToolRegistry
from repopilot.tools.search import SearchCodeTool


def test_explorer_exposes_only_readonly_tools_and_rejects_write(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("bad", "write_file", {
            "path": "owned.py", "content": "bad = True\n",
        })]),
        LLMResponse(text=(
            '{"language":"python","frameworks":[],"test_command":"pytest -q",'
            '"important_files":["app.py"],"structure_summary":"one Python module",'
            '"build_system":"unknown"}'
        )),
    ])
    bus = EventBus("explorer-test")
    # Include a write tool in the registry to prove the agent allowlist remains
    # an enforcement boundary even if a caller constructs a broader registry.
    registry = ToolRegistry([
        GlobTool(), ReadFileTool(), SearchCodeTool(), GitStatusTool(), WriteFileTool(),
    ])
    runtime = AgentRuntime(llm, registry, PermissionManager(tmp_path), bus)
    explorer = RepositoryExplorer(
        runtime,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "explorer-test", bus),
        tmp_path / "artifacts",
    )

    result = asyncio.run(explorer.inspect("pytest -q"))

    assert result.language == "python"
    assert not (tmp_path / "owned.py").exists()
    assert {tool["name"] for tool in llm.requests[0][1]} == {
        "glob", "read_file", "search_code", "git_status",
    }
    tool_messages = [message for message in llm.requests[1][0] if message.role == "tool"]
    assert "tool_not_allowed" in tool_messages[-1].content


def test_local_fallback_includes_implementation_and_test_sources(tmp_path) -> None:
    (tmp_path / "parser.py").write_text("def parse(): pass\n", encoding="utf-8")
    (tmp_path / "test_parser.py").write_text("def test_parse(): pass\n", encoding="utf-8")

    result = RepositoryExplorer.local_fallback(tmp_path, "pytest -q")

    assert result.important_files == ["parser.py", "test_parser.py"]
