import asyncio

import pytest

from repopilot.core.errors import DuplicateToolError
from repopilot.core.models import ToolCall, ToolResult
from repopilot.tools.registry import ToolRegistry


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = name

    def schema(self):
        return {"name": self.name}

    async def execute(self, arguments, context):
        return ToolResult(True, arguments["value"])


def test_registry_preserves_order_and_dispatches() -> None:
    registry = ToolRegistry([FakeTool("a"), FakeTool("b")])
    assert [schema["name"] for schema in registry.schemas()] == ["a", "b"]
    result = asyncio.run(registry.execute(ToolCall("1", "b", {"value": "ok"}), None))
    assert result == ToolResult(True, "ok")
    assert asyncio.run(registry.execute(ToolCall("2", "missing", {}), None)).error_code == "unknown_tool"


def test_duplicate_names_rejected() -> None:
    with pytest.raises(DuplicateToolError):
        ToolRegistry([FakeTool("a"), FakeTool("a")])

