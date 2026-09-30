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
    evidence_keys: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class HypothesisCheck(Serializable):
    """A root-cause assumption and a concrete way to try to disprove it."""

    key: str
    claim: str
    falsification: str
    evidence_keys: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Plan(Serializable):
    summary: str
    steps: List[PlanStep]
    root_cause_hypothesis: str = ""
    earliest_invariant: str = ""
    impact_surface: List[str] = field(default_factory=list)
    hypothesis_checks: List[HypothesisCheck] = field(default_factory=list)

    def validate(self, required_behavior_keys: Iterable[str] = (),
                 available_evidence_keys: Iterable[str] = ()) -> None:
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
            if len(step.evidence_keys) != len(set(step.evidence_keys)):
                raise ValueError("plan step evidence keys must be unique")
        required = set(required_behavior_keys)
        covered = {key for step in self.steps for key in step.behavior_keys}
        if covered - required:
            raise ValueError("plan references an unknown behavior case")
        if required:
            if not self.root_cause_hypothesis.strip() or not self.earliest_invariant.strip():
                raise ValueError("behavior-aware plan requires a causal hypothesis and invariant")
            if required - covered:
                raise ValueError("plan does not cover every behavior case")
            if not self.hypothesis_checks:
                raise ValueError("behavior-aware plan requires a falsifiable hypothesis check")
        check_keys = [item.key for item in self.hypothesis_checks]
        if len(check_keys) != len(set(check_keys)) or any(not key for key in check_keys):
            raise ValueError("hypothesis check keys must be non-empty and unique")
        for item in self.hypothesis_checks:
            if not item.claim.strip() or not item.falsification.strip():
                raise ValueError("hypothesis checks require a claim and falsification")
            if len(item.evidence_keys) != len(set(item.evidence_keys)):
                raise ValueError("hypothesis check evidence keys must be unique")
        available_evidence = set(available_evidence_keys)
        cited_evidence = {key for step in self.steps for key in step.evidence_keys}
        cited_evidence.update(
            key for item in self.hypothesis_checks for key in item.evidence_keys
        )
        if cited_evidence - available_evidence:
            raise ValueError("plan references unknown repository evidence")
        if available_evidence and not cited_evidence:
            raise ValueError("repository-grounded plan must cite observed evidence")
