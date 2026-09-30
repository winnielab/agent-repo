"""Workspace-contained writes and conflict-safe exact edits."""

from __future__ import annotations

from typing import Any, Dict

from repopilot.core.models import ToolResult
from repopilot.execution.paths import safe_workspace_path
from repopilot.task.intent import is_test_path
from repopilot.tools.base import ToolContext, function_schema


def _test_change_guard(path: str, context: ToolContext) -> ToolResult | None:
    if context.values.get("allow_test_changes", True) or not is_test_path(path):
        return None
    return ToolResult(
        False,
        (
            f"Test artifact changes are outside this task contract: {path}. "
            "Fix production behavior and use the configured suite as evidence."
        ),
        {"path": path},
        "test_change_not_allowed",
    )


class WriteFileTool:
    name = "write_file"
    description = "Create or replace a UTF-8 text file inside the workspace."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        relative_input = str(arguments["path"])
        guard = _test_change_guard(relative_input, context)
        if guard:
            return guard
        path = safe_workspace_path(context.workspace, relative_input)
        content = arguments["content"]
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        relative = path.relative_to(context.workspace.resolve()).as_posix()
        context.event_bus.emit("file_changed", path=relative, operation="create" if before is None else "replace",
                               before_chars=len(before or ""), after_chars=len(content))
        return ToolResult(True, f"Wrote {len(content)} characters to {relative}", {"path": relative})


class EditFileTool:
    name = "edit_file"
    description = "Replace exact text in one file; succeeds only when the old text occurs exactly once."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        relative_input = str(arguments["path"])
        guard = _test_change_guard(relative_input, context)
        if guard:
            return guard
        path = safe_workspace_path(context.workspace, relative_input, must_exist=True)
        old_text, new_text = arguments["old_text"], arguments["new_text"]
        if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
            raise ValueError("old_text must be non-empty and both values must be strings")
        original = path.read_text(encoding="utf-8")
        occurrences = original.count(old_text)
        if occurrences != 1:
            return ToolResult(
                False,
                f"Expected old_text exactly once in {arguments['path']}; found {occurrences}",
                {"occurrences": occurrences},
                "edit_conflict",
            )
        updated = original.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        relative = path.relative_to(context.workspace.resolve()).as_posix()
        context.event_bus.emit("file_changed", path=relative, operation="edit",
                               before_chars=len(original), after_chars=len(updated))
        return ToolResult(True, f"Edited {relative}", {"path": relative})


class DeleteFileTool:
    name = "delete_file"
    description = "Delete one regular file inside the isolated workspace; directories are never removed."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        relative_input = str(arguments["path"])
        guard = _test_change_guard(relative_input, context)
        if guard:
            return guard
        path = safe_workspace_path(context.workspace, relative_input, must_exist=True)
        if not path.is_file() or path.is_symlink():
            return ToolResult(False, f"Refusing to delete a non-regular file: {arguments['path']}",
                              error_code="not_regular_file")
        size = path.stat().st_size
        relative = path.relative_to(context.workspace.resolve()).as_posix()
        path.unlink()
        context.event_bus.emit("file_changed", path=relative, operation="delete",
                               before_chars=size, after_chars=0)
        return ToolResult(True, f"Deleted {relative}", {"path": relative, "deleted_bytes": size})
