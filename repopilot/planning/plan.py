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
    kind: str = "dependency_fact"
    behavior_keys: List[str] = field(default_factory=list)

    KINDS = frozenset({"dependency_fact", "candidate_semantics"})


@dataclass(frozen=True)
class Plan(Serializable):
    summary: str
    steps: List[PlanStep]
    root_cause_hypothesis: str = ""
    earliest_invariant: str = ""
    impact_surface: List[str] = field(default_factory=list)
    hypothesis_checks: List[HypothesisCheck] = field(default_factory=list)
    active_behavior_keys: List[str] = field(default_factory=list)
    deferred_behavior_keys: List[str] = field(default_factory=list)
    temporal_rationale: str = ""

    def behavior_partition(self, required_behavior_keys: Iterable[str]) -> tuple[set[str], set[str]]:
        """Return the active/deferred partition with backward-compatible defaults."""
        required = set(required_behavior_keys)
        active = set(self.active_behavior_keys)
        deferred = set(self.deferred_behavior_keys)
        if required and not active and not deferred:
            active = set(required)
        return active, deferred

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
        active, deferred = self.behavior_partition(required)
        if active & deferred or active | deferred != required:
            raise ValueError("plan behavior phase partition must cover every behavior exactly once")
        covered = {key for step in self.steps for key in step.behavior_keys}
        if covered - active:
            raise ValueError("plan references an unknown behavior case")
        if active:
            if not self.root_cause_hypothesis.strip() or not self.earliest_invariant.strip():
                raise ValueError("behavior-aware plan requires a causal hypothesis and invariant")
            if active - covered:
                raise ValueError("plan does not cover every active behavior case")
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
            if item.kind not in item.KINDS:
                raise ValueError("hypothesis check has an unsupported kind")
            if len(item.behavior_keys) != len(set(item.behavior_keys)):
                raise ValueError("hypothesis check behavior keys must be unique")
            if set(item.behavior_keys) - active:
                raise ValueError("hypothesis check references an inactive behavior case")
        if active:
            semantic_coverage = {
                key
                for item in self.hypothesis_checks
                if item.kind == "candidate_semantics"
                for key in item.behavior_keys
            }
            if active - semantic_coverage:
                raise ValueError("candidate semantic checks must cover every active behavior case")
        available_evidence = set(available_evidence_keys)
        cited_evidence = {key for step in self.steps for key in step.evidence_keys}
        cited_evidence.update(
            key for item in self.hypothesis_checks for key in item.evidence_keys
        )
        if cited_evidence - available_evidence:
            raise ValueError("plan references unknown repository evidence")
        if available_evidence and not cited_evidence:
            raise ValueError("repository-grounded plan must cite observed evidence")
