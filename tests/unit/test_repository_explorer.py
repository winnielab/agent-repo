import asyncio

from repopilot.core.models import LLMResponse, ToolCall
from repopilot.core.runtime import AgentRuntime
from repopilot.execution.environment import LocalEnvironment
from repopilot.execution.permission import PermissionManager
from repopilot.llm.scripted import ScriptedLLM
from repopilot.observability.events import EventBus
from repopilot.repo.explorer import RepositoryExplorer
from repopilot.task.specification import TaskSpecification
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


def test_explorer_builds_task_focused_source_backed_slice(tmp_path) -> None:
    (tmp_path / "parser.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
    (tmp_path / "test_parser.py").write_text(
        "from parser import parse\n\ndef test_parse():\n    assert parse(1) == 1\n",
        encoding="utf-8",
    )
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("find", "search_code", {
            "query": "parse", "path": ".", "max_matches": 20,
        })]),
        LLMResponse(text='''{
          "language":"python","frameworks":[],"test_command":"pytest -q",
          "important_files":[],"structure_summary":"parser plus adjacent tests",
          "build_system":"unknown","target_symbols":["parse"],
          "implementation_files":["parser.py"],"test_files":["test_parser.py"],
          "instruction_files":[],"observations":[
            {"key":"definition_parse","kind":"definition","path":"parser.py","line":1,
             "symbol":"parse","detail":"implementation entry point"},
            {"key":"test_parse","kind":"test","path":"test_parser.py","line":3,
             "symbol":"test_parse","detail":"adjacent behavior contract"}
          ]
        }'''),
    ])
    bus = EventBus("focused-explorer")
    runtime = AgentRuntime(
        llm,
        ToolRegistry([GlobTool(), ReadFileTool(), SearchCodeTool(), GitStatusTool()]),
        PermissionManager(tmp_path),
        bus,
    )
    explorer = RepositoryExplorer(
        runtime,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "focused-explorer", bus),
        tmp_path / "artifacts",
    )
    specification = TaskSpecification(
        "Preserve false-like parser names", "Fix parse without changing numeric inputs",
        ["False-like names work", "pytest -q passes"], [],
    )

    result = asyncio.run(explorer.inspect("pytest -q", specification))

    assert result.target_symbols == ["parse"]
    assert result.implementation_files == ["parser.py"]
    assert [item.key for item in result.observations] == ["definition_parse", "test_parse"]
    assert "Preserve false-like parser names" in llm.requests[0][0][-1].content


def test_explorer_downgrades_temporal_phase_without_version_evidence(tmp_path) -> None:
    (tmp_path / "app.py").write_text("LEGACY = True\n", encoding="utf-8")
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("read", "read_file", {"path": "app.py"})]),
        LLMResponse(text='''{
          "language":"python","frameworks":[],"test_command":"pytest -q",
          "important_files":["app.py"],"structure_summary":"one implementation file",
          "build_system":"unknown","implementation_files":["app.py"],
          "observations":[
            {"key":"legacy_clause","kind":"definition","path":"app.py","line":1,
             "symbol":"LEGACY","detail":"legacy behavior is still present"}
          ],
          "active_temporal_phase":"current",
          "temporal_rationale":"The legacy clause is present.",
          "temporal_evidence_keys":["legacy_clause"]
        }'''),
    ])
    bus = EventBus("temporal-explorer")
    runtime = AgentRuntime(
        llm,
        ToolRegistry([GlobTool(), ReadFileTool(), SearchCodeTool(), GitStatusTool()]),
        PermissionManager(tmp_path),
        bus,
    )
    explorer = RepositoryExplorer(
        runtime,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "temporal-explorer", bus),
        tmp_path / "artifacts",
    )

    result = asyncio.run(explorer.inspect("pytest -q"))

    assert result.active_temporal_phase == "unknown"
    assert "checkout version or release phase" in result.temporal_rationale
