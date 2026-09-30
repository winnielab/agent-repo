"""Independent acceptance-criteria evaluator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from repopilot.agents.contracts import contract_violations, patch_hygiene_violations
from repopilot.agents.reviewer import ReviewResult
from repopilot.core.models import Message, Serializable
from repopilot.core.structured import parse_json_object
from repopilot.llm.base import LLMClient
from repopilot.observability.events import EventBus
from repopilot.observability.usage import UsageTracker
from repopilot.planning.plan import Plan
from repopilot.task.specification import TaskSpecification
from repopilot.task.intent import requests_test_changes


@dataclass(frozen=True)
class CriterionResult(Serializable):
    criterion: str
    status: str
    evidence: str
    key: str = ""
    expected: str = ""
    observed: str = ""
    counterexample: str = ""
    invalidated_assumption: str = ""


@dataclass(frozen=True)
class EvaluationResult(Serializable):
    passed: bool
    criteria: List[CriterionResult] = field(default_factory=list)
    summary: str = ""

    def repair_feedback(self) -> str:
        failures = [item for item in self.criteria if item.status != "pass"]
        blocks = []
        for item in failures:
            details = [
                f"- [{item.status.upper()}] {item.key or item.criterion}: {item.criterion}",
                f"  Expected: {item.expected or item.criterion}",
                f"  Observed: {item.observed or item.evidence or 'not established'}",
            ]
            if item.counterexample:
                details.append(f"  Counterexample: {item.counterexample}")
            if item.invalidated_assumption:
                details.append(f"  Invalidated assumption: {item.invalidated_assumption}")
            details.append(f"  Evidence: {item.evidence or 'insufficient evidence'}")
            blocks.append("\n".join(details))
        return "\n".join(blocks)


class GoalEvaluator:
    VALID = {"pass", "fail", "unknown"}

    @staticmethod
    def _bounded_text(value: Any, limit: int = 12_000) -> str:
        text = str(value)
        if len(text) <= limit:
            return text
        half = (limit - 64) // 2
        return text[:half] + "\n... bounded for goal evaluation ...\n" + text[-half:]

    @classmethod
    def _test_evidence(cls, result: Dict[str, Any]) -> Dict[str, Any]:
        compact = {
            key: value for key, value in result.items()
            if key not in {"stdout", "stderr", "output"}
        }
        # Defend the independent evaluation boundary against stale or
        # third-party metadata that contradicts an authoritative successful
        # process result.  This also keeps replayed pre-fix reports from
        # confusing the evaluator.
        if result.get("ok", False):
            compact["current_failure_ids"] = []
            compact["new_failure_ids"] = []
            compact["baseline_compatibility_errors"] = []
            compact["current_compatibility_errors"] = []
            compact["compatibility_errors"] = []
        compact["output"] = cls._bounded_text(result.get("output", ""))
        return compact

    @classmethod
    def _test_failure_evidence(cls, result: Dict[str, Any]) -> str:
        compatibility_errors = result.get("compatibility_errors") or []
        if compatibility_errors:
            missing = ", ".join(
                f"{item.get('module', '?')}.{item.get('symbol', '?')}"
                for item in compatibility_errors
            )
            return cls._bounded_text(
                "Target-version compatibility failure: the candidate uses unavailable "
                f"symbol(s) {missing}. Replace speculative APIs with definitions verified "
                "in the current checkout/environment.\n\n"
                + str(result.get("output", "tests failed")),
                2_000,
            )
        return cls._bounded_text(result.get("output", "tests failed"), 2_000)

    def __init__(self, llm: LLMClient, event_bus: EventBus, usage: UsageTracker) -> None:
        self.llm = llm
        self.event_bus = event_bus
        self.usage = usage

    async def evaluate(self, specification: TaskSpecification, diff: str,
                       test_result: Dict[str, Any], review: ReviewResult,
                       repository_evidence: Optional[Dict[str, Any]] = None,
                       plan: Optional[Plan] = None) -> EvaluationResult:
        targets = [
            {
                "key": f"acceptance:{index}",
                "criterion": criterion,
                "expected": criterion,
            }
            for index, criterion in enumerate(specification.acceptance_criteria)
        ]
        targets.extend({
            "key": f"behavior:{item.key}",
            "criterion": (
                f"[{item.dimension}] WHEN {item.scenario} => EXPECT {item.expected}"
                + (f"; PRESERVE {item.must_preserve}" if item.must_preserve else "")
                + (f"; STATE/SIDE EFFECT {item.state_effect}" if item.state_effect else "")
            ),
            "expected": item.expected,
        } for item in specification.behavior_cases)
        if plan is not None:
            targets.extend({
                "key": f"hypothesis:{item.key}",
                "criterion": (
                    f"ROOT-CAUSE CLAIM {item.claim}; FALSIFICATION CHECK {item.falsification}"
                ),
                "expected": (
                    "The claim is supported by specific source/command evidence and the "
                    "falsification attempt did not contradict it."
                ),
            } for item in plan.hypothesis_checks)
        payload = {
            "task": specification.to_dict(), "diff": diff[-50_000:],
            "tests": self._test_evidence(test_result), "review": review.to_dict(),
            "repository_evidence": repository_evidence or {},
            "plan": plan.to_dict() if plan is not None else {},
            "evaluation_targets": targets,
            "schema": {"criteria": [{
                "criterion_key": "exact key from evaluation_targets",
                "status": "pass|fail|unknown",
                "evidence": "specific repository/diff/test evidence",
                "expected": "required observable behavior",
                "observed": "behavior established by current evidence",
                "counterexample": "concrete conflicting input/path/evidence, or empty string",
                "invalidated_assumption": "candidate assumption disproved by the evidence, or empty string",
            }], "summary": "string"},
        }
        self.event_bus.emit("goal_evaluation_started", criteria=len(targets))
        criteria: List[CriterionResult] = []
        try:
            response = await self.llm.generate([
                Message("system", "Independently and skeptically evaluate every evaluation_target and return JSON only. Passing visible tests is necessary but not sufficient for semantic constraints. Treat wording literally, including negative, preservation, input-category, and temporal/version behavior. Broader exception handling, input acceptance, side effects, or API behavior is a failure when the task requests a narrow case. For a hypothesis target, distinguish evidence that merely repeats the claim from an observation capable of falsifying it; mark unknown when the proposed probe/source trace was not performed and source evidence does not directly settle the claim, and fail when command/source evidence contradicts it. For fail or unknown, separate expected behavior from observed behavior and provide the strongest concrete counterexample plus the candidate assumption it invalidates. Source-level class, inheritance, signature, data-flow, and __slots__ contracts are specific evidence when the claimed behavior follows directly from them; do not require a redundant test in that case. Use unknown when evidence is genuinely insufficient; never turn a plausible implementation claim into pass without specific diff, test, review, or repository evidence."),
                Message("user", json.dumps(payload, ensure_ascii=False)),
            ])
            self.usage.add_usage(response.usage)
            data = parse_json_object(response.text)
            keyed = {
                str(item.get("criterion_key", "")).strip(): item
                for item in data["criteria"]
                if str(item.get("criterion_key", "")).strip()
            }
            indexed = {
                int(item["criterion_index"]): item
                for item in data["criteria"]
                if "criterion_index" in item
            }
            for target in targets:
                item = keyed.get(target["key"])
                if item is None and target["key"].startswith("acceptance:"):
                    item = indexed.get(int(target["key"].split(":", 1)[1]))
                if item is None:
                    criteria.append(CriterionResult(
                        target["criterion"],
                        "unknown",
                        "Evaluator returned no result for this behavior contract.",
                        key=target["key"],
                        expected=target["expected"],
                        observed="not evaluated",
                        invalidated_assumption="coverage cannot be assumed without explicit evidence",
                    ))
                    continue
                status = str(item["status"]).lower()
                if status not in self.VALID:
                    raise ValueError("invalid criterion status")
                criteria.append(CriterionResult(
                    target["criterion"],
                    status,
                    self._bounded_text(item.get("evidence", ""), 4_000),
                    key=target["key"],
                    expected=self._bounded_text(item.get("expected") or target["expected"], 2_000),
                    observed=self._bounded_text(item.get("observed", ""), 2_000),
                    counterexample=self._bounded_text(item.get("counterexample", ""), 2_000),
                    invalidated_assumption=self._bounded_text(
                        item.get("invalidated_assumption", ""), 2_000,
                    ),
                ))
            summary = self._bounded_text(data.get("summary", ""), 4_000)
        except Exception as exc:
            criteria = [CriterionResult(
                target["criterion"],
                "unknown",
                f"Evaluator could not establish evidence: {exc}",
                key=target["key"],
                expected=target["expected"],
                observed="not evaluated",
                invalidated_assumption="semantic coverage cannot be assumed",
            ) for target in targets]
            summary = "Evaluation response was invalid; criteria remain unknown."

        if not test_result.get("ok", False):
            failure_ids = test_result.get("new_failure_ids") or test_result.get("current_failure_ids") or []
            test_evidence = self._test_failure_evidence(test_result)
            criteria.append(CriterionResult(
                "Configured tests pass",
                "fail",
                test_evidence,
                key="tests:configured",
                expected="The configured test contract has no candidate-introduced failure.",
                observed="The configured test command failed or introduced a new failure.",
                counterexample=", ".join(map(str, failure_ids[:20])) or test_evidence[:1_000],
                invalidated_assumption="the candidate preserves the configured regression contract",
            ))
        if review.decision != "approve":
            review_summary = review.raw_artifacts.get("summary", "review rejected")
            criteria.append(CriterionResult(
                "Reviewer approves the patch",
                "fail",
                review_summary,
                key="review:approval",
                expected="No blocking correctness issue remains in the candidate diff.",
                observed="The independent reviewer rejected the patch.",
                counterexample=str(review_summary)[:1_000],
                invalidated_assumption="the diff has no independently detectable blocking issue",
            ))
        if not diff.strip():
            criteria.append(CriterionResult(
                "Patch contains an implementation change",
                "fail",
                "Git diff is empty",
                key="patch:nonempty",
                expected="The repository contains a focused implementation change.",
                observed="Git diff is empty.",
                counterexample="empty candidate diff",
                invalidated_assumption="analysis alone completes a coding task",
            ))
        violations = contract_violations(
            diff,
            list((repository_evidence or {}).get("normative_contracts", [])),
        )
        for violation in violations:
            location = f"{violation['contract_file']}:{violation['contract_line']}"
            criteria.append(CriterionResult(
                "Candidate preserves explicit source contracts",
                "fail",
                (
                    f"{location} says `{violation['contract_text']}`, but the diff adds "
                    f"`{violation['added_line']}`."
                ),
                key=f"contract:{violation['kind']}:{violation['symbol']}",
                expected=f"{violation['symbol']} continues to propagate as explicitly documented.",
                observed=f"The candidate catches {violation['symbol']}.",
                counterexample=violation["contract_text"],
                invalidated_assumption=(
                    "preserving an existing catch is safe despite an explicit propagation contract"
                ),
            ))
        for violation in patch_hygiene_violations(
            diff,
            allow_test_changes=requests_test_changes(specification),
            requested_text="\n".join([
                specification.title,
                specification.description,
                *specification.constraints,
            ]),
        ):
            criteria.append(CriterionResult(
                "Candidate patch contains only intended deliverables",
                "fail",
                f"{violation['file']}: {violation['detail']}",
                key=f"patch_hygiene:{violation['kind']}:{violation['file']}",
                expected="No temporary artifacts or out-of-contract test changes are included.",
                observed=f"Patch includes {violation['file']}.",
                counterexample=violation["file"],
                invalidated_assumption="every file currently present in git diff is an intended deliverable",
            ))
        passed = bool(criteria) and all(item.status == "pass" for item in criteria)
        result = EvaluationResult(passed, criteria, summary)
        self.event_bus.emit("goal_evaluation_finished", passed=passed,
                            results=[item.to_dict() for item in criteria])
        return result
