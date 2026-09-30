"""Create a validated plan from task and repository context."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from repopilot.core.models import Message
from repopilot.core.structured import parse_json_object
from repopilot.llm.base import LLMClient
from repopilot.observability.events import EventBus
from repopilot.observability.usage import UsageTracker
from repopilot.planning.plan import HypothesisCheck, Plan, PlanStep
from repopilot.repo.explorer import RepositoryContext
from repopilot.task.intent import requests_test_changes
from repopilot.task.specification import TaskSpecification


class Planner:
    def __init__(self, llm: LLMClient, event_bus: EventBus, usage: UsageTracker) -> None:
        self.llm = llm
        self.event_bus = event_bus
        self.usage = usage

    @staticmethod
    def fallback(specification: TaskSpecification,
                 repository: Optional[RepositoryContext] = None) -> Plan:
        behavior_keys = [item.key for item in specification.behavior_cases]
        evidence_keys = [item.key for item in (repository.observations if repository else [])]
        impact_surface = list(dict.fromkeys(
            value
            for item in specification.behavior_cases
            for value in (item.must_preserve, item.state_effect)
            if value.strip()
        ))
        return Plan(
            "Trace the issue-described behavior to the earliest violated invariant, then implement one focused fix.",
            [PlanStep(
                "implement",
                specification.title,
                specification.description,
                [],
                behavior_keys,
                evidence_keys,
            )],
            "The defect is in the earliest repository control-flow or data-normalization point that contradicts the issue.",
            "Issue-described inputs must retain their required meaning before downstream formatting, adaptation, or output.",
            impact_surface,
            [HypothesisCheck(
                "trace-earliest-violation",
                "The proposed earliest invariant is the first point that contradicts the requested behavior.",
                "Trace one issue-described input through the definition, a caller or state transition, and the observable output; use a focused read-only probe when source semantics are ambiguous, and reject the claim if an earlier divergence appears.",
                evidence_keys,
            )] if behavior_keys else [],
        )

    @staticmethod
    def _requests_test_changes(specification: TaskSpecification) -> bool:
        return requests_test_changes(specification)

    @staticmethod
    def _is_test_only(step: PlanStep) -> bool:
        text = f"{step.title}\n{step.description}".casefold()
        patterns = (
            r"\b(add|write|create|update|modify|extend|fix)\b.{0,50}\b(test|tests|coverage)\b",
            r"^(test|tests|testing|coverage|regression test)\b",
            r"(新增|添加|编写|修改|更新|补充|修复).{0,30}(测试|覆盖率)",
            r"^(测试|覆盖率)",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    @classmethod
    def _without_nonimplementation_steps(
        cls,
        steps: list[PlanStep],
        allow_test_changes: bool,
    ) -> list[PlanStep]:
        # The workflow always runs the configured suite after workers finish.
        # A standalone verification task burns a fresh model context without
        # producing repository progress, so fold it into the coding task.
        verification_prefixes = (
            "run ", "verify", "validate", "check", "confirm", "inspect", "investigate",
        )
        implementation_words = ("add", "fix", "implement", "update", "modify", "restore", "remove")

        def verification_only(step: PlanStep) -> bool:
            title = step.title.strip().casefold()
            if title.startswith(verification_prefixes):
                return True
            has_verification = any(
                re.search(rf"\b{word}\b", title)
                for word in ("verify", "validate", "confirm", "check", "inspect", "investigate")
            )
            has_implementation = any(re.search(rf"\b{word}\b", title) for word in implementation_words)
            return has_verification and not has_implementation

        removed = {
            step.key for step in steps
            if verification_only(step)
            or (not allow_test_changes and cls._is_test_only(step))
        }
        if not removed:
            return steps
        by_key = {step.key: step for step in steps}

        def actionable_dependencies(key: str, seen: set[str]) -> list[str]:
            if key not in removed:
                return [key]
            if key in seen:
                raise ValueError("verification step dependency cycle")
            expanded: list[str] = []
            for dependency in by_key[key].dependencies:
                expanded.extend(actionable_dependencies(dependency, seen | {key}))
            return expanded

        normalized = []
        for step in steps:
            if step.key in removed:
                continue
            dependencies = []
            for dependency in step.dependencies:
                dependencies.extend(actionable_dependencies(dependency, set()))
            normalized.append(PlanStep(
                step.key,
                step.title,
                step.description,
                list(dict.fromkeys(dependencies)),
                step.behavior_keys,
                step.evidence_keys,
            ))
        if not normalized:
            raise ValueError("plan contains no implementation task")
        return normalized

    async def create_plan(self, specification: TaskSpecification, repository: RepositoryContext,
                          repository_evidence: Optional[Dict[str, Any]] = None) -> Plan:
        payload = {
            "task": specification.to_dict(),
            "repository": repository.to_dict(),
            "repository_evidence": repository_evidence or {},
            "schema": {
                "summary": "string",
                "root_cause_hypothesis": "causal explanation grounded in repository evidence",
                "earliest_invariant": "earliest data/control-flow property that the fix must restore",
                "impact_surface": ["public behavior, state, caller, or failure path that must remain compatible"],
                "hypothesis_checks": [{
                    "key": "short-unique-key",
                    "claim": "a material root-cause assumption",
                    "falsification": "specific source trace or focused read-only probe that could disprove it",
                    "evidence_keys": ["repository observation keys supporting the check"],
                }],
                "steps": [{"key": "short-unique-key", "title": "string",
                           "description": "actionable coding task", "dependencies": ["earlier-key"],
                           "behavior_keys": ["behavior case keys covered by this step"],
                           "evidence_keys": ["repository observation keys supporting this step"]}],
            },
        }
        self.event_bus.emit("planning_started")
        try:
            response = await self.llm.generate([
                Message("system", "Create a small executable coding plan. Return JSON only. State a repository-grounded root_cause_hypothesis and the earliest_invariant whose first violation explains the behavior; prefer fixing that earliest point over a late output-layer workaround. For every behavior-aware plan, add at least one hypothesis_check naming a material claim and a concrete source trace or focused read-only probe that could disprove it. A check must be capable of changing the implementation plan; repeating the claim is not falsification. Cite repository observation keys for implementation claims and checks, and list the impact_surface that must remain compatible, including callers, state transitions, and failure paths supported by the evidence. Map every supplied behavior case key to at least one implementation step. Plan implementation work, plus test-writing only when the issue explicitly requests tests or coverage. Never modify tests merely to prove a fix. The workflow runs the configured suite after all workers, so never create a standalone run/verify/check step. Treat positive, negative, temporal/version, preservation, and state-effect requirements literally. Prefer the narrowest behavior change, never add input-specific branches merely to satisfy examples, and never broaden exception handling or public API behavior unless explicitly required. Every dependency/framework symbol used by the plan must be verifiable in the current checkout."),
                Message("user", json.dumps(payload, ensure_ascii=False)),
            ])
            self.usage.add_usage(response.usage)
            data = parse_json_object(response.text)
            steps = [PlanStep(
                key=str(item["key"]).strip(), title=str(item["title"]).strip(),
                description=str(item["description"]).strip(),
                dependencies=[str(dep).strip() for dep in item.get("dependencies", [])],
                behavior_keys=[str(key).strip() for key in item.get("behavior_keys", [])],
                evidence_keys=[str(key).strip() for key in item.get("evidence_keys", [])],
            ) for item in data["steps"]]
            steps = self._without_nonimplementation_steps(
                steps,
                allow_test_changes=self._requests_test_changes(specification),
            )
            checks = [HypothesisCheck(
                key=str(item["key"]).strip(),
                claim=str(item["claim"]).strip(),
                falsification=str(item["falsification"]).strip(),
                evidence_keys=[str(key).strip() for key in item.get("evidence_keys", [])],
            ) for item in data.get("hypothesis_checks", [])]
            plan = Plan(
                str(data["summary"]).strip(),
                steps,
                str(data.get("root_cause_hypothesis", "")).strip(),
                str(data.get("earliest_invariant", "")).strip(),
                [str(item).strip() for item in data.get("impact_surface", []) if str(item).strip()],
                checks,
            )
            plan.validate(
                (item.key for item in specification.behavior_cases),
                (item.key for item in repository.observations),
            )
            self.event_bus.emit("planning_finished", fallback=False, steps=len(steps))
            return plan
        except Exception as exc:
            plan = self.fallback(specification, repository)
            self.event_bus.emit("planning_finished", fallback=True, steps=1,
                                error=f"{type(exc).__name__}: {exc}")
            return plan
