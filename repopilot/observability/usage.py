"""Usage and tool-call aggregation kept outside the runtime loop."""

from dataclasses import dataclass, field
from typing import Dict

from repopilot.core.models import Usage


@dataclass
class ToolMetric:
    calls: int = 0
    succeeded: int = 0
    failed: int = 0
    duration_seconds: float = 0.0


@dataclass
class UsageTracker:
    usage: Usage = field(default_factory=Usage)
    tools: Dict[str, ToolMetric] = field(default_factory=dict)

    def add_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage

    def record_tool(self, name: str, ok: bool, duration_seconds: float) -> None:
        metric = self.tools.setdefault(name, ToolMetric())
        metric.calls += 1
        metric.succeeded += int(ok)
        metric.failed += int(not ok)
        metric.duration_seconds += duration_seconds

    @property
    def tool_calls(self) -> int:
        return sum(metric.calls for metric in self.tools.values())

