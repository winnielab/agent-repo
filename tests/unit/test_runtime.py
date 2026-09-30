import asyncio

from repopilot.core.agent import Agent
from repopilot.core.context import ContextManager
from repopilot.core.models import AgentTask, LLMResponse, ToolCall, ToolResult, Usage
from repopilot.core.runtime import AgentRuntime, RuntimeLimits
from repopilot.execution.environment import LocalEnvironment
from repopilot.execution.permission import PermissionManager
from repopilot.llm.scripted import ScriptedLLM
from repopilot.observability.events import EventBus, MemorySubscriber
from repopilot.tools.base import ToolContext
from repopilot.tools.registry import ToolRegistry


class EchoTool:
    name = "echo"
    description = "echo"

    def schema(self):
        return {"name": self.name, "description": "echo", "parameters": {"type": "object"}}

    async def execute(self, arguments, context):
        context.values["executions"] = context.values.get("executions", 0) + 1
        return ToolResult(True, arguments.get("value", ""))


class PassingTestsTool(EchoTool):
    name = "run_tests"


class EditingTool(EchoTool):
    name = "edit_file"


class AllowAll(PermissionManager):
    async def authorize(self, call):
        from repopilot.execution.permission import PermissionDecision
        return True, PermissionDecision.ALLOW, "test"


def test_runtime_handles_two_tool_rounds_then_text(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("1", "echo", {"value": "a"})], usage=Usage(3, 1)),
        LLMResponse(tool_calls=[ToolCall("2", "echo", {"value": "b"}), ToolCall("3", "missing", {})], usage=Usage(4, 1)),
        LLMResponse(text="done", usage=Usage(2, 1), stop_reason="stop"),
    ])
    memory = MemorySubscriber()
    bus = EventBus("run", [memory])
    task = AgentTask("test")
    context = ContextManager("system", task, tmp_path, bus)
    runtime = AgentRuntime(llm, ToolRegistry([EchoTool()]), AllowAll(tmp_path), bus,
                           limits=RuntimeLimits(10, 10, 1000, 10))
    result = asyncio.run(runtime.run(
        Agent("test", "system", ["echo", "missing"]), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus),
    ))
    assert result.status == "completed" and result.iterations == 3
    assert result.usage.input_tokens == 9
    tool_messages = [message for message in context.messages if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["1", "2", "3"]
    assert any("unknown_tool" in message.content for message in tool_messages)


def test_agent_tool_allowlist_is_enforced_before_registry(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("1", "echo", {"value": "blocked"})]),
        LLMResponse(text="done"),
    ])
    bus = EventBus("run")
    task = AgentTask("test")
    context = ContextManager("system", task, tmp_path, bus)
    values = {"executions": 0}
    result = asyncio.run(AgentRuntime(
        llm, ToolRegistry([EchoTool()]), AllowAll(tmp_path), bus,
    ).run(
        Agent("readonly", "system", []), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus, values),
    ))
    assert result.status == "completed"
    assert values["executions"] == 0
    assert llm.requests[0][1] == []
    assert "tool_not_allowed" in [
        message for message in context.messages if message.role == "tool"
    ][0].content


def test_runtime_limit_is_structured(tmp_path) -> None:
    llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall("1", "echo", {})])])
    bus = EventBus("run")
    task = AgentTask("test")
    context = ContextManager("system", task, tmp_path, bus)
    runtime = AgentRuntime(llm, ToolRegistry([EchoTool()]), AllowAll(tmp_path), bus,
                           limits=RuntimeLimits(1, 10, 1000, 10))
    result = asyncio.run(runtime.run(
        Agent("test", "system", ["echo"]), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus),
    ))
    assert result.status == "limit_reached"
    assert "max_iterations" in result.error


def test_denied_tool_is_returned_to_model_without_execution(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("1", "echo", {})]),
        LLMResponse(text="observed denial"),
    ])
    bus = EventBus("run")
    task = AgentTask("test")
    context = ContextManager("system", task, tmp_path, bus)
    values = {"executions": 0}
    tool_context = ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus, values)
    result = asyncio.run(AgentRuntime(
        llm, ToolRegistry([EchoTool()]), PermissionManager(tmp_path), bus,
    ).run(Agent("test", "system", ["echo"]), task, context, tool_context))
    assert result.status == "completed"
    assert values["executions"] == 0
    assert "permission_denied" in [message for message in context.messages if message.role == "tool"][0].content


def test_runtime_auto_stops_after_one_post_test_follow_up(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("tests", "run_tests", {})]),
        LLMResponse(tool_calls=[ToolCall("extra", "echo", {"value": "unneeded"})]),
    ])
    memory = MemorySubscriber()
    bus = EventBus("run", [memory])
    task = AgentTask("test")
    context = ContextManager("system", task, tmp_path, bus)
    result = asyncio.run(AgentRuntime(
        llm, ToolRegistry([PassingTestsTool(), EchoTool()]), AllowAll(tmp_path), bus,
        limits=RuntimeLimits(10, 10, 1000, 10, max_post_test_iterations=1),
    ).run(
        Agent("coder", "system", ["run_tests", "echo"]), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus),
    ))
    assert result.status == "completed"
    assert result.iterations == 2
    assert len(llm.requests) == 2
    assert any(event["event_type"] == "agent_auto_stopped" for event in memory.events)


def test_runtime_reminds_coder_to_edit_after_read_only_turns(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("1", "echo", {})]),
        LLMResponse(tool_calls=[ToolCall("2", "echo", {})]),
        LLMResponse(tool_calls=[ToolCall("3", "edit_file", {})]),
        LLMResponse(text="done"),
    ])
    memory = MemorySubscriber()
    bus = EventBus("run", [memory])
    task = AgentTask("change code")
    context = ContextManager("system", task, tmp_path, bus)
    result = asyncio.run(AgentRuntime(
        llm, ToolRegistry([EchoTool(), EditingTool()]), AllowAll(tmp_path), bus,
        limits=RuntimeLimits(10, 10, 1000, 10, max_no_mutation_iterations=2),
    ).run(
        Agent("coder", "system", ["echo", "edit_file"]), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus),
    ))
    assert result.status == "completed"
    assert any("Implementation checkpoint" in message.content
               for message in llm.requests[2][0])
    assert [schema["name"] for schema in llm.requests[2][1]] == ["edit_file"]
    assert any(event["event_type"] == "agent_mutation_reminder" for event in memory.events)


def test_runtime_rejects_second_empty_patch_completion(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(text="No change needed"),
        LLMResponse(text="Still no change needed"),
    ])
    bus = EventBus("run")
    task = AgentTask("change code")
    context = ContextManager("system", task, tmp_path, bus)
    result = asyncio.run(AgentRuntime(
        llm, ToolRegistry([EditingTool()]), AllowAll(tmp_path), bus,
        limits=RuntimeLimits(10, 10, 1000, 10),
    ).run(
        Agent("coder", "system", ["edit_file"]), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus),
    ))
    assert result.status == "limit_reached"
    assert result.error == "runtime limit reached: no_repository_mutation"


def test_runtime_enters_edit_checkpoint_before_token_budget_is_exhausted(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("1", "echo", {})], usage=Usage(80, 0)),
        LLMResponse(tool_calls=[ToolCall("2", "edit_file", {})]),
        LLMResponse(text="done"),
    ])
    memory = MemorySubscriber()
    bus = EventBus("run", [memory])
    task = AgentTask("change code")
    context = ContextManager("system", task, tmp_path, bus)
    result = asyncio.run(AgentRuntime(
        llm, ToolRegistry([EchoTool(), EditingTool()]), AllowAll(tmp_path), bus,
        limits=RuntimeLimits(10, 10, 100, 10, max_no_mutation_iterations=8),
    ).run(
        Agent("coder", "system", ["echo", "edit_file"]), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus),
    ))
    assert result.status == "completed"
    assert [schema["name"] for schema in llm.requests[1][1]] == ["edit_file"]
    assert any(event["event_type"] == "agent_mutation_reminder"
               and event["data"].get("token_pressure") for event in memory.events)


def test_runtime_defers_empty_patch_completion_once(tmp_path) -> None:
    llm = ScriptedLLM([
        LLMResponse(text="I am done"),
        LLMResponse(tool_calls=[ToolCall("edit", "edit_file", {})]),
        LLMResponse(text="implemented"),
    ])
    memory = MemorySubscriber()
    bus = EventBus("run", [memory])
    task = AgentTask("change code")
    context = ContextManager("system", task, tmp_path, bus)
    result = asyncio.run(AgentRuntime(
        llm, ToolRegistry([EditingTool()]), AllowAll(tmp_path), bus,
        limits=RuntimeLimits(10, 10, 1000, 10),
    ).run(
        Agent("coder", "system", ["edit_file"]), task, context,
        ToolContext(tmp_path, LocalEnvironment(tmp_path), "run", bus),
    ))
    assert result.status == "completed"
    assert result.iterations == 3
    assert any(event["event_type"] == "agent_empty_patch_completion_deferred"
               for event in memory.events)
