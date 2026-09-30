"""Declarative agent configuration; loops belong to AgentRuntime."""

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class Agent:
    name: str
    system_prompt: str
    tool_names: List[str] = field(default_factory=list)

