"""A bounded shell tool; permission is enforced by AgentRuntime."""

from typing import Any, Dict

from repopilot.core.models import ToolResult
from repopilot.tools.base import ToolContext, function_schema


class BashTool:
    name = "bash"
    description = "Run a non-interactive shell command in the workspace. Dangerous commands are denied by policy."

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {"command": {"type": "string"}, "timeout": {"type": "number", "minimum": 0.1, "maximum": 120}},
            "required": ["command"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        command = arguments["command"]
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        result = await context.environment.execute(command, timeout=arguments.get("timeout"))
        output = (result.stdout + result.stderr).strip() or "(no output)"
        evidence = context.values.setdefault("command_evidence", [])
        evidence.append({
            "command": command[:1000],
            "ok": result.ok,
            "returncode": result.returncode,
            "output": output[-2000:],
        })
        if len(evidence) > 10:
            del evidence[:-10]
        return ToolResult(result.ok, output, result.to_dict(), None if result.ok else "command_failed")
