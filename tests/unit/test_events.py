import json

from repopilot.core.models import Usage
from repopilot.observability.events import EventBus, JSONLSubscriber, MemorySubscriber
from repopilot.observability.usage import UsageTracker


def test_event_reaches_multiple_subscribers_and_jsonl_appends(tmp_path) -> None:
    memory = MemorySubscriber()
    path = tmp_path / "run.jsonl"
    bus = EventBus("run-1", [memory, JSONLSubscriber(path)])
    bus.emit("tool_called", command="<redacted>")
    bus.emit("tool_finished", ok=True)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(memory.events) == len(rows) == 2
    assert rows[0]["data"]["command"] == "<redacted>"


def test_usage_tracker_accumulates() -> None:
    tracker = UsageTracker()
    tracker.add_usage(Usage(10, 3, 2, 0.1))
    tracker.add_usage(Usage(4, 2, 0, 0.2))
    tracker.record_tool("read_file", True, 0.25)
    assert tracker.usage.input_tokens == 14
    assert tracker.usage.cost == pytest.approx(0.3)
    assert tracker.tool_calls == 1


import pytest

