"""Stable tool registration and dispatch."""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, List

from repopilot.core.errors import DuplicateToolError
from repopilot.core.models import ToolCall, ToolResult
from repopilot.tools.base import Tool, ToolContext


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: "OrderedDict[str, Tool]" = OrderedDict()
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name or tool.name in self._tools:
            raise DuplicateToolError(f"tool already registered or invalid: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names_with_capability(self, capability: str) -> set[str]:
        """Discover tool behavior without teaching the runtime concrete names."""
        return {
            name for name, tool in self._tools.items()
            if bool(getattr(tool, capability, False))
        }

    def schemas(self, names: Iterable[str] = ()) -> List[Dict]:
        selected = set(names)
        return [tool.schema() for name, tool in self._tools.items() if not selected or name in selected]

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(False, f"Unknown tool: {call.name}", error_code="unknown_tool")
        if not isinstance(call.arguments, dict):
            return ToolResult(False, "Tool arguments must be an object", error_code="invalid_arguments")
        try:
            result = await tool.execute(call.arguments, context)
            if not isinstance(result, ToolResult):
                return ToolResult(False, "Tool returned an invalid result", error_code="invalid_tool_result")
            return result
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(False, f"Invalid arguments: {exc}", error_code="invalid_arguments")
        except Exception as exc:
            return ToolResult(False, f"Tool failed: {type(exc).__name__}: {exc}", error_code="tool_error")
