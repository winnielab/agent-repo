"""Tool contracts."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Protocol

from repopilot.core.models import ToolResult
from repopilot.execution.environment import ExecutionEnvironment
from repopilot.observability.events import EventBus


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    environment: ExecutionEnvironment
    run_id: str
    event_bus: EventBus
    values: Dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    description: str

    def schema(self) -> Dict[str, Any]:
        ...

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        ...


def function_schema(name: str, description: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "description": description, "parameters": parameters}

