"""Normalized user goal shared by planning, review, and evaluation."""

from dataclasses import dataclass, field
import re
from typing import List

from repopilot.core.models import Serializable


@dataclass(frozen=True)
class BehaviorCase(Serializable):
    """Issue-derived behavior that must be covered without inventing tests."""

    key: str
    scenario: str
    expected: str
    must_preserve: str = ""
    dimension: str = "positive"
    state_effect: str = ""

    DIMENSIONS = frozenset({
        "positive", "negative", "boundary", "temporal", "preservation", "failure_path",
    })

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", self.key):
            raise ValueError("behavior case key must be a short lowercase identifier")
        if not self.scenario.strip() or not self.expected.strip():
            raise ValueError("behavior cases require scenario and expected behavior")
        if self.dimension not in self.DIMENSIONS:
            raise ValueError("behavior case has an unsupported semantic dimension")


@dataclass(frozen=True)
class TaskSpecification(Serializable):
    title: str
    description: str
    acceptance_criteria: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    behavior_cases: List[BehaviorCase] = field(default_factory=list)

    def validate(self) -> None:
        if not self.title.strip() or not self.description.strip():
            raise ValueError("task specification requires title and description")
        if not self.acceptance_criteria or any(not item.strip() for item in self.acceptance_criteria):
            raise ValueError("task specification requires non-empty acceptance criteria")
        if not 2 <= len(self.acceptance_criteria) <= 5:
            raise ValueError("task specification must contain two to five focused acceptance criteria")
        normalized = [item.strip().casefold() for item in self.acceptance_criteria]
        if len(normalized) != len(set(normalized)):
            raise ValueError("task acceptance criteria must be unique")
        if any(not item.strip() for item in self.constraints):
            raise ValueError("task constraints cannot be blank")
        if len(self.behavior_cases) > 8:
            raise ValueError("task specification supports at most eight focused behavior cases")
        for behavior in self.behavior_cases:
            behavior.validate()
        behavior_keys = [item.key for item in self.behavior_cases]
        if len(behavior_keys) != len(set(behavior_keys)):
            raise ValueError("behavior case keys must be unique")
