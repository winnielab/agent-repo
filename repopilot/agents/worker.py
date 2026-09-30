"""Run the shared Coder against one persistent TaskRecord."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from repopilot.agents.coder import CoderAgent
from repopilot.core.context import ContextManager
from repopilot.core.models import AgentResult, AgentTask, Serializable
from repopilot.core.runtime import AgentRuntime
from repopilot.memory.working import WorkingMemory
from repopilot.planning.plan import Plan
from repopilot.task.graph import TaskRecord
from repopilot.task.specification import TaskSpecification
from repopilot.tools.base import ToolContext


@dataclass(frozen=True)
class WorkerResult(Serializable):
    task_id: str
    status: str
    final_text: str
    iterations: int
    error: str = ""


class CoderWorker:
    def __init__(self, runtime: AgentRuntime, tool_context: ToolContext, artifact_dir: Path,
                 context_char_budget: int = 80_000,
                 large_tool_result_chars: int = 12_000) -> None:
        self.runtime = runtime
        self.tool_context = tool_context
        self.artifact_dir = artifact_dir
        self.context_char_budget = context_char_budget
        self.large_tool_result_chars = large_tool_result_chars

    async def run(self, record: TaskRecord, specification: TaskSpecification,
                  memory: WorkingMemory, feedback: str = "",
                  plan: Optional[Plan] = None,
                  repository_evidence: Optional[Dict[str, Any]] = None,
                  ) -> tuple[WorkerResult, AgentResult]:
        behavior_contract = "\n".join(
            f"- [{item.key}/{item.dimension}] WHEN {item.scenario} => EXPECT {item.expected}"
            + (f"; PRESERVE {item.must_preserve}" if item.must_preserve else "")
            + (f"; STATE/SIDE EFFECT {item.state_effect}" if item.state_effect else "")
            for item in specification.behavior_cases
        )
        causal_contract = ""
        if plan is not None:
            causal_contract = (
                f"Root-cause hypothesis: {plan.root_cause_hypothesis or 'not established'}\n"
                f"Earliest invariant to restore: {plan.earliest_invariant or 'not established'}\n"
                "Impact surface to preserve:\n- "
                + "\n- ".join(plan.impact_surface or ["not established"])
            )
        hypothesis_contract = ""
        if plan is not None and plan.hypothesis_checks:
            hypothesis_contract = "\n".join(
                f"- [{item.key}] CLAIM {item.claim}\n"
                f"  TRY TO DISPROVE WITH: {item.falsification}\n"
                f"  GROUNDED BY: {', '.join(item.evidence_keys) or 'no cited observation'}"
                for item in plan.hypothesis_checks
            )
        observations = (repository_evidence or {}).get("observations", [])
        observation_contract = "\n".join(
            f"- [{item.get('key', '?')}/{item.get('kind', '?')}] "
            f"{item.get('path', '?')}:{item.get('line', '?')} "
            f"{item.get('symbol', '')} — {item.get('detail', '')}".rstrip()
            for item in observations[:16]
        )
        normative_contracts = (repository_evidence or {}).get("normative_contracts", [])
        source_contract = "\n".join(
            f"- {item.get('file', '?')}:{item.get('line', '?')}: {item.get('text', '')}"
            for item in normative_contracts[:50]
        )
        description = "\n\n".join(filter(None, [
            f"Issue: {specification.title}\n{specification.description}",
            "Acceptance criteria:\n- " + "\n- ".join(specification.acceptance_criteria),
            "Behavior contract:\n" + behavior_contract if behavior_contract else "",
            "Causal plan:\n" + causal_contract if causal_contract else "",
            (
                "Hypothesis falsification contract (perform before editing; if a check fails, "
                "revise the causal path instead of patching the planned output):\n" + hypothesis_contract
                if hypothesis_contract else ""
            ),
            "Task-relevant repository evidence:\n" + observation_contract
            if observation_contract else "",
            "Explicit source contracts (must not be contradicted):\n" + source_contract
            if source_contract else "",
            f"Current task [{record.id}] {record.title}:\n{record.description}",
            "Working memory:\n" + memory.prompt_context(),
            (
                "Repair contract (treat each counterexample as evidence that the prior "
                "assumption is invalid; revise the causal hypothesis before editing):\n" + feedback
                if feedback else ""
            ),
        ]))
        task = AgentTask(description, {"task_id": record.id})
        agent = CoderAgent()
        context = ContextManager(agent.system_prompt, task, self.artifact_dir / record.id,
                                 self.runtime.event_bus, self.context_char_budget,
                                 self.large_tool_result_chars)
        result = await self.runtime.run(agent, task, context, self.tool_context)
        worker = WorkerResult(record.id, result.status, result.final_text, result.iterations,
                              result.error or "")
        if result.success:
            memory.remember("decisions", f"Completed {record.title}: {result.final_text[:500]}")
        else:
            memory.remember("failed_attempts", f"{record.title}: {result.error or result.status}")
        return worker, result
