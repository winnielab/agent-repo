from repopilot.core.context import ContextManager
from repopilot.core.models import AgentTask, ToolCall
from repopilot.observability.events import EventBus, MemorySubscriber


def test_large_tool_result_is_persisted(tmp_path) -> None:
    context = ContextManager("system", AgentTask("task"), tmp_path, EventBus("run"), large_result_chars=10)
    context.add_tool_result(ToolCall("call/1", "bash", {}), "x" * 100)
    assert "persisted-output" in context.messages[-1].content
    assert list((tmp_path / "tool-results").glob("*.txt"))


def test_compaction_keeps_task_and_complete_recent_tool_turn(tmp_path) -> None:
    memory = MemorySubscriber()
    bus = EventBus("run", [memory])
    context = ContextManager("system", AgentTask("current task"), tmp_path, bus,
                             char_budget=500, keep_recent_messages=2)
    for number in range(5):
        call = ToolCall(str(number), "read_file", {"path": "a"})
        context.add_assistant("", [call])
        context.add_tool_result(call, "result " + ("x" * 150))
    assert context.compact_if_needed()
    built = context.build_context()
    assert built[1].content == "current task"
    assert context.messages[0].role == "assistant"
    assert context.messages[1].role == "tool"
    assert any(event["type"] == "context_compacted" for event in memory.events)

