"""Serializable workflow and step state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from repopilot.core.models import JSONValue, Serializable


class WorkflowStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class StepState(Serializable):
    key: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    attempt: int = 0
    output: Optional[JSONValue] = None
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "StepState":
        return cls(
            key=str(data["key"]),
            status=WorkflowStatus(str(data.get("status", "pending"))),
            attempt=int(data.get("attempt", 0)),
            output=data.get("output"),  # type: ignore[arg-type]
            error=str(data["error"]) if data.get("error") is not None else None,
        )


@dataclass
class WorkflowState(Serializable):
    run_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_step: Optional[str] = None
    steps: Dict[str, StepState] = field(default_factory=dict)
    completed_order: List[str] = field(default_factory=list)
    journal_sequence: int = 0
    journal_checksum: str = ""
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "WorkflowState":
        raw_steps = data.get("steps", {})
        steps = {
            str(key): StepState.from_dict(value)  # type: ignore[arg-type]
            for key, value in dict(raw_steps).items()  # type: ignore[arg-type]
        }
        return cls(
            run_id=str(data["run_id"]),
            status=WorkflowStatus(str(data.get("status", "pending"))),
            current_step=(str(data["current_step"])
                          if data.get("current_step") is not None else None),
            steps=steps,
            completed_order=[str(item) for item in data.get("completed_order", [])],  # type: ignore[union-attr]
            journal_sequence=int(data.get("journal_sequence", 0)),
            journal_checksum=str(data.get("journal_checksum", "")),
            error=str(data["error"]) if data.get("error") is not None else None,
        )
