"""Structured implementation plan."""

from dataclasses import dataclass, field
from typing import Iterable, List

from repopilot.core.models import Serializable


@dataclass(frozen=True)
class PlanStep(Serializable):
    key: str
    title: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    behavior_keys: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Plan(Serializable):
    summary: str
    steps: List[PlanStep]
    root_cause_hypothesis: str = ""
    earliest_invariant: str = ""

    def validate(self, required_behavior_keys: Iterable[str] = ()) -> None:
        if not self.summary.strip() or not self.steps:
            raise ValueError("plan requires a summary and at least one step")
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)) or any(not key for key in keys):
            raise ValueError("plan step keys must be non-empty and unique")
        known = set(keys)
        for step in self.steps:
            if not step.title.strip() or not step.description.strip():
                raise ValueError("plan steps require title and description")
            if step.key in step.dependencies or any(dep not in known for dep in step.dependencies):
                raise ValueError("plan contains an invalid dependency")
            if len(step.behavior_keys) != len(set(step.behavior_keys)):
                raise ValueError("plan step behavior keys must be unique")
        required = set(required_behavior_keys)
        covered = {key for step in self.steps for key in step.behavior_keys}
        if covered - required:
            raise ValueError("plan references an unknown behavior case")
        if required:
            if not self.root_cause_hypothesis.strip() or not self.earliest_invariant.strip():
                raise ValueError("behavior-aware plan requires a causal hypothesis and invariant")
            if required - covered:
                raise ValueError("plan does not cover every behavior case")
