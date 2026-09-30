"""JSON-safe contracts used by the entire harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Union

JSONScalar = Union[str, int, float, bool, None]
JSONValue = Union[JSONScalar, List["JSONValue"], Dict[str, "JSONValue"]]


def json_safe(value: Any) -> JSONValue:
    """Recursively convert supported domain values into JSON-safe values."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class Serializable:
    def to_dict(self) -> Dict[str, JSONValue]:
        converted = json_safe(self)
        if not isinstance(converted, dict):
            raise TypeError("serializable model did not produce an object")
        return converted


@dataclass(frozen=True)
class ToolCall(Serializable):
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class Message(Serializable):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_call_id: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class Usage(Serializable):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: Optional[float] = None

    def __add__(self, other: "Usage") -> "Usage":
        costs = [value for value in (self.cost, other.cost) if value is not None]
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cost=sum(costs) if costs else None,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class LLMResponse(Serializable):
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: Optional[str] = None
    raw: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, JSONValue]:
        result = super().to_dict()
        result.pop("raw", None)
        return result


@dataclass(frozen=True)
class ToolResult(Serializable):
    ok: bool
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None


@dataclass(frozen=True)
class AgentTask(Serializable):
    description: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult(Serializable):
    status: Literal["completed", "failed", "limit_reached", "needs_input"]
    final_text: str
    iterations: int
    usage: Usage = field(default_factory=Usage)
    changed_files: List[str] = field(default_factory=list)
    diff: str = ""
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class CommandResult(Serializable):
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

