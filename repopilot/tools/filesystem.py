"""Workspace-contained file reading and globbing."""

from __future__ import annotations

import glob
from typing import Any, Dict

from repopilot.core.models import ToolResult
from repopilot.execution.paths import safe_workspace_path
from repopilot.tools.base import ToolContext, function_schema


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file with 1-based line numbers and an optional range."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path = safe_workspace_path(context.workspace, str(arguments["path"]), must_exist=True)
        if not path.is_file():
            return ToolResult(False, f"Not a file: {arguments['path']}", error_code="not_file")
        offset = int(arguments.get("offset", 1))
        limit = min(int(arguments.get("limit", 400)), 2000)
        if offset < 1 or limit < 1:
            raise ValueError("offset and limit must be positive")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset - 1: offset - 1 + limit]
        content = "\n".join(f"{number:>6}\t{line}" for number, line in enumerate(selected, offset))
        if offset - 1 + limit < len(lines):
            content += f"\n... {len(lines) - (offset - 1 + limit)} more lines"
        relative = path.relative_to(context.workspace.resolve()).as_posix()
        read_files = context.values.setdefault("read_files", [])
        if relative not in read_files and len(read_files) < 100:
            read_files.append(relative)
        return ToolResult(True, content or "(empty file)", {"path": relative, "total_lines": len(lines)})


class GlobTool:
    name = "glob"
    description = "List workspace files matching a glob pattern; ** is recursive."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 1000}},
            "required": ["pattern"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = str(arguments["pattern"])
        if pattern.startswith("/") or ".." in pattern.split("/"):
            return ToolResult(False, "Glob pattern escapes workspace", error_code="path_escape")
        limit = min(int(arguments.get("max_results", 200)), 1000)
        excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".repopilot"}
        matches = []
        for raw in glob.iglob(pattern, root_dir=str(context.workspace), recursive=True):
            path = safe_workspace_path(context.workspace, raw)
            relative = path.relative_to(context.workspace.resolve())
            if path.is_file() and not any(part in excluded for part in relative.parts):
                matches.append(relative.as_posix())
        matches = sorted(set(matches))
        shown = matches[:limit]
        content = "\n".join(shown) if shown else "(no matches)"
        if len(matches) > limit:
            content += f"\n... {len(matches) - limit} more matches"
        return ToolResult(True, content, {"matches": len(matches)})
