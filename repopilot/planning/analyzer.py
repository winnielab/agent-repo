"""Convert a raw issue into a validated task specification."""

from __future__ import annotations

import json
from typing import Optional

from repopilot.core.models import Message
from repopilot.core.structured import parse_json_object
from repopilot.llm.base import LLMClient
from repopilot.observability.events import EventBus
from repopilot.observability.usage import UsageTracker
from repopilot.task.specification import BehaviorCase, TaskSpecification


class TaskAnalyzer:
    def __init__(self, llm: LLMClient, event_bus: EventBus, usage: UsageTracker) -> None:
        self.llm = llm
        self.event_bus = event_bus
        self.usage = usage

    @staticmethod
    def fallback(issue: str, test_command: str) -> TaskSpecification:
        title = issue.strip().splitlines()[0][:120] or "Repository issue"
        return TaskSpecification(
            title=title,
            description=issue.strip(),
            acceptance_criteria=[
                "The behavior described by the issue is implemented.",
                f"The configured test command passes: {test_command}",
                "Existing public behavior has no unintended regression.",
            ],
            constraints=["Keep changes focused on the issue.", "Modify files only inside the isolated worktree."],
            behavior_cases=[BehaviorCase(
                "issue_behavior",
                "Conditions and inputs explicitly described by the issue",
                "The issue-described behavior is implemented literally",
                "Behavior outside the issue-described scope remains unchanged",
                "positive",
                "Issue-visible state and failure-path side effects remain compatible",
            )],
        )

    async def analyze(self, issue: str, test_command: str) -> TaskSpecification:
        fallback = self.fallback(issue, test_command)
        prompt = {
            "issue": issue,
            "test_command": test_command,
            "required_schema": {
                "title": "string", "description": "string",
                "acceptance_criteria": ["specific observable criterion"],
                "constraints": ["implementation constraint"],
                "behavior_cases": [{
                    "key": "short_lowercase_id",
                    "dimension": "positive|negative|boundary|temporal|preservation|failure_path",
                    "scenario": "issue-stated condition or input category",
                    "expected": "observable expected behavior",
                    "must_preserve": "explicitly unchanged behavior, or empty string",
                    "state_effect": "required state/side effect after success or failure, or empty string",
                }],
            },
        }
        self.event_bus.emit("task_analysis_started")
        try:
            response = await self.llm.generate([
                Message("system", "Analyze the software issue. Return one JSON object only with 2-5 concise, observable acceptance criteria and 1-8 behavior_cases. Include the configured test command passing as an acceptance criterion, but do not create a behavior case for merely running that command. A behavior case is not a test case: it is an issue-derived runtime scenario, its expected behavior, any behavior the issue explicitly says must remain unchanged, and any required state or side effect after success or failure. Classify each row as positive, negative, boundary, temporal, preservation, or failure_path. Split positive/negative, current/future version, input-category, and failure-state branches only when the issue states or directly implies them. Do not invent edge cases, exception types, input classes, or hidden-test guesses. Do not copy the issue verbatim as the only criterion."),
                Message("user", json.dumps(prompt, ensure_ascii=False)),
            ])
            self.usage.add_usage(response.usage)
            data = parse_json_object(response.text)
            behavior_cases = [BehaviorCase(
                key=str(item["key"]).strip(),
                scenario=str(item["scenario"]).strip(),
                expected=str(item["expected"]).strip(),
                must_preserve=str(item.get("must_preserve", "")).strip(),
                dimension=str(item.get("dimension", "positive")).strip(),
                state_effect=str(item.get("state_effect", "")).strip(),
            ) for item in data.get("behavior_cases", [])]
            if not behavior_cases:
                raise ValueError("task analysis requires at least one issue-derived behavior case")
            specification = TaskSpecification(
                title=str(data["title"]).strip(),
                description=str(data["description"]).strip(),
                acceptance_criteria=[str(item).strip() for item in data["acceptance_criteria"]],
                constraints=[str(item).strip() for item in data.get("constraints", [])],
                behavior_cases=behavior_cases,
            )
            specification.validate()
            self.event_bus.emit("task_analysis_finished", fallback=False,
                                criteria=len(specification.acceptance_criteria))
            return specification
        except Exception as exc:
            self.event_bus.emit("task_analysis_finished", fallback=True, error=f"{type(exc).__name__}: {exc}")
            return fallback
