"""Read-only Git observations scoped to the worktree."""

from typing import Any, Dict

from repopilot.core.models import ToolResult
from repopilot.tools.base import ToolContext, function_schema


class GitDiffTool:
    name = "git_diff"
    description = "Show the current workspace diff against HEAD."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object", "properties": {}, "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        if arguments:
            return ToolResult(False, "git_diff takes no arguments", error_code="invalid_arguments")
        result = await context.environment.execute("git diff --no-ext-diff HEAD --")
        return ToolResult(result.ok, result.stdout or "(no changes)", result.to_dict(),
                          None if result.ok else "git_failed")


class GitStatusTool:
    name = "git_status"
    description = "Show concise Git status for the current workspace."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object", "properties": {}, "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        result = await context.environment.execute("git status --short")
        return ToolResult(result.ok, result.stdout or "(clean)", result.to_dict(),
                          None if result.ok else "git_failed")
