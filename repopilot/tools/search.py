"""Deterministic text search without shell interpolation."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict

from repopilot.core.models import ToolResult
from repopilot.execution.paths import safe_workspace_path
from repopilot.tools.base import ToolContext, function_schema


class SearchCodeTool:
    name = "search_code"
    description = "Search text files for a literal or regular expression and return file:line:text matches."
    EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".repopilot"}

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "glob": {"type": "string"},
                "regex": {"type": "boolean", "default": False},
                "max_matches": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["query"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        query = str(arguments["query"])
        root = safe_workspace_path(context.workspace, str(arguments.get("path", ".")), must_exist=True)
        matcher = re.compile(query) if arguments.get("regex", False) else re.compile(re.escape(query))
        pattern = arguments.get("glob")
        limit = min(int(arguments.get("max_matches", 100)), 500)
        candidates = [root] if root.is_file() else root.rglob("*")
        matches = []
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(context.workspace.resolve())
            if any(part in self.EXCLUDED for part in relative.parts):
                continue
            if pattern and not fnmatch.fnmatch(relative.as_posix(), str(pattern)):
                continue
            if path.stat().st_size > 2_000_000:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, 1):
                if matcher.search(line):
                    matches.append(f"{relative.as_posix()}:{line_number}:{line[:500]}")
                    if len(matches) >= limit:
                        return ToolResult(True, "\n".join(matches), {"matches": len(matches), "truncated": True})
        return ToolResult(True, "\n".join(matches) if matches else "(no matches)", {"matches": len(matches)})

