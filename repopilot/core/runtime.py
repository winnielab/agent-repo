"""One stable model → tool calls → results loop."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

from repopilot.core.agent import Agent
from repopilot.core.context import ContextManager
from repopilot.core.hooks import HookEvent, HookManager
from repopilot.core.models import AgentResult, AgentTask, ToolResult
from repopilot.execution.permission import PermissionDecision, PermissionManager
from repopilot.llm.base import LLMClient
from repopilot.observability.events import EventBus
from repopilot.observability.usage import UsageTracker
from repopilot.tools.base import ToolContext
from repopilot.tools.registry import ToolRegistry


def _missing_required_verification(tool_context: ToolContext) -> list[str]:
    """Return checks without supported evidence for the current candidate revision."""
    required = {
        str(key) for key in tool_context.values.get("required_verification_keys", []) if str(key)
    }
    if not required:
        return []
    revision = int(tool_context.values.get("mutation_revision", 0))
    records = {
        str(item.get("check_key")): item
        for item in tool_context.values.get("verification_evidence", [])
        if isinstance(item, dict) and item.get("check_key")
    }
    return sorted(
        key for key in required
        if key not in records
        or records[key].get("verdict") != "supported"
        or int(records[key].get("mutation_revision", -1)) != revision
    )


@dataclass(frozen=True)
class RuntimeLimits:
    max_iterations: int = 30
    max_tool_calls: int = 120
    max_tokens: int = 200_000
    max_runtime_seconds: float = 120.0
    max_post_test_iterations: int = 1
    max_no_mutation_iterations: int = 0
    max_readonly_iterations: int = 0


class AgentRuntime:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        permission_manager: PermissionManager,
        event_bus: EventBus,
        hooks: Optional[HookManager] = None,
        usage: Optional[UsageTracker] = None,
        limits: Optional[RuntimeLimits] = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.permission_manager = permission_manager
        self.event_bus = event_bus
        self.hooks = hooks or HookManager()
        self.usage = usage or UsageTracker()
        self.limits = limits or RuntimeLimits()

    async def run(
        self,
        agent: Agent,
        task: AgentTask,
        context: ContextManager,
        tool_context: ToolContext,
    ) -> AgentResult:
        started = time.monotonic()
        iterations = 0
        final_text = ""
        successful_test_iteration: Optional[int] = None
        mutation_seen = False
        mutation_reminders = 0
        readonly_reminder = False
        completion_deferred = False
        verification_reminder: tuple[str, ...] = ()
        last_input_tokens = 0
        mutation_tools = self.registry.names_with_capability("mutates_workspace")
        validation_tools = self.registry.names_with_capability("validates_workspace")
        coding_agent = bool(mutation_tools.intersection(agent.tool_names))
        readonly_agent = bool(agent.tool_names) and not coding_agent
        self.event_bus.emit("agent_loop_started", agent=agent.name, task=task.description)
        # Benchmark-compatible lifecycle name.
        self.event_bus.emit("agent_started", agent=agent.name, workspace=str(tool_context.workspace))
        try:
            while True:
                elapsed = time.monotonic() - started
                readonly_checkpoint = bool(
                    readonly_agent
                    and self.limits.max_readonly_iterations
                    and iterations >= self.limits.max_readonly_iterations
                )
                if readonly_checkpoint and not readonly_reminder:
                    context.add_observation(
                        "Read-only exploration checkpoint: synthesize the required final result now "
                        "from the evidence already collected. Do not request another tool. Mark any "
                        "unestablished relation as unknown instead of expanding the repository scan."
                    )
                    readonly_reminder = True
                    self.event_bus.emit(
                        "agent_readonly_finalize_requested",
                        iterations=iterations,
                        threshold=self.limits.max_readonly_iterations,
                    )
                if (coding_agent and not mutation_seen and self.limits.max_no_mutation_iterations
                        and iterations >= self.limits.max_no_mutation_iterations
                        and mutation_reminders == 0):
                    context.add_observation(
                        "Implementation checkpoint: no repository change has been made yet. "
                        "Stop broad exploration, use the evidence already collected to make the "
                        "smallest plausible source edit now, then run the configured tests."
                    )
                    mutation_reminders = 1
                    self.event_bus.emit(
                        "agent_mutation_reminder", iterations=iterations,
                        threshold=self.limits.max_no_mutation_iterations,
                    )
                if (coding_agent and not mutation_seen and self.limits.max_no_mutation_iterations
                        and iterations >= self.limits.max_no_mutation_iterations + 3
                        and mutation_reminders == 1):
                    context.add_observation(
                        "Final implementation checkpoint: the next useful action must be an "
                        "edit_file/write_file call or a concise, concrete blocker. Do not read "
                        "additional broad files or history."
                    )
                    mutation_reminders = 2
                    self.event_bus.emit(
                        "agent_mutation_reminder", iterations=iterations,
                        threshold=self.limits.max_no_mutation_iterations + 3,
                        final=True,
                    )
                missing_verification = _missing_required_verification(tool_context)
                if (successful_test_iteration is not None
                        and iterations - successful_test_iteration
                        >= self.limits.max_post_test_iterations
                        and not missing_verification):
                    self.event_bus.emit(
                        "agent_auto_stopped",
                        reason="post_test_iteration_limit",
                        iterations=iterations,
                        successful_test_iteration=successful_test_iteration,
                    )
                    result = AgentResult("completed", final_text, iterations, self.usage.usage)
                    self.event_bus.emit("agent_loop_finished", status=result.status,
                                        duration_seconds=elapsed)
                    return result
                if (successful_test_iteration is not None
                        and iterations - successful_test_iteration
                        >= self.limits.max_post_test_iterations
                        and missing_verification
                        and tuple(missing_verification) != verification_reminder):
                    verification_reminder = tuple(missing_verification)
                    context.add_observation(
                        "Completion gate: tests passed, but these required candidate-semantic "
                        "checks lack supported evidence at the current mutation revision: "
                        f"{', '.join(missing_verification)}. Run the focused probes/source traces "
                        "and call record_evidence before finishing."
                    )
                    self.event_bus.emit(
                        "agent_verification_completion_deferred",
                        checks=missing_verification,
                        mutation_revision=int(tool_context.values.get("mutation_revision", 0)),
                    )
                limit_reason = None
                if iterations >= self.limits.max_iterations:
                    limit_reason = "max_iterations"
                elif self.usage.tool_calls >= self.limits.max_tool_calls:
                    limit_reason = "max_tool_calls"
                elif self.usage.usage.total_tokens >= self.limits.max_tokens:
                    limit_reason = "max_tokens"
                elif elapsed >= self.limits.max_runtime_seconds:
                    limit_reason = "max_runtime_seconds"
                if limit_reason:
                    self.event_bus.emit("limit_reached", limit=limit_reason, iterations=iterations,
                                        tool_calls=self.usage.tool_calls)
                    result = AgentResult("limit_reached", final_text, iterations, self.usage.usage,
                                         error=f"runtime limit reached: {limit_reason}")
                    self.event_bus.emit("agent_loop_finished", status=result.status, duration_seconds=elapsed)
                    return result
                messages = context.build_context()
                remaining_tokens = self.limits.max_tokens - self.usage.usage.total_tokens
                token_checkpoint = bool(
                    coding_agent
                    and not mutation_seen
                    and self.limits.max_no_mutation_iterations
                    and last_input_tokens
                    and remaining_tokens <= max(
                        self.limits.max_tokens // 5,
                        last_input_tokens * 3,
                    )
                )
                if token_checkpoint and mutation_reminders == 0:
                    context.add_observation(
                        "Token-budget implementation checkpoint: the remaining worker budget is "
                        "only enough for a few model turns. Use edit_file/write_file now with "
                        "the evidence already collected; do not request another read-only tool."
                    )
                    messages = context.build_context()
                    mutation_reminders = 1
                    self.event_bus.emit(
                        "agent_mutation_reminder", iterations=iterations,
                        remaining_tokens=remaining_tokens, token_pressure=True,
                    )
                # An empty agent allowlist means the agent has no tools.  The
                # registry's public API keeps an empty selection as "all" for
                # callers that want discovery, so enforce the agent contract
                # at the runtime boundary as well as when dispatching calls.
                mutation_checkpoint = bool(
                    coding_agent
                    and not mutation_seen
                    and self.limits.max_no_mutation_iterations
                    and (
                        iterations >= self.limits.max_no_mutation_iterations
                        or token_checkpoint
                    )
                )
                active_tool_names = (
                    []
                    if readonly_checkpoint
                    else [name for name in agent.tool_names if name in mutation_tools]
                    if mutation_checkpoint
                    else agent.tool_names
                )
                schemas = self.registry.schemas(active_tool_names) if active_tool_names else []
                if mutation_checkpoint:
                    self.event_bus.emit(
                        "agent_tools_restricted",
                        iterations=iterations,
                        tools=active_tool_names,
                        reason="mutation_checkpoint",
                    )
                elif readonly_checkpoint:
                    self.event_bus.emit(
                        "agent_tools_restricted",
                        iterations=iterations,
                        tools=[],
                        reason="readonly_finalize_checkpoint",
                    )
                iteration = iterations + 1
                await self.hooks.run(HookEvent.BEFORE_LLM_CALL, iteration=iteration, messages=messages)
                self.event_bus.emit("llm_called", iteration=iteration, message_count=len(messages))
                response = await self.llm.generate(messages, schemas)
                iterations = iteration
                self.usage.add_usage(response.usage)
                last_input_tokens = response.usage.input_tokens
                self.event_bus.emit(
                    "llm_finished",
                    iteration=iteration,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cached_tokens=response.usage.cached_tokens,
                    stop_reason=response.stop_reason,
                    tool_calls=len(response.tool_calls),
                )
                await self.hooks.run(HookEvent.AFTER_LLM_CALL, iteration=iteration, response=response)
                context.add_assistant(response.text, response.tool_calls)
                final_text = response.text or final_text

                if not response.tool_calls:
                    if coding_agent and not mutation_seen and not completion_deferred:
                        completion_deferred = True
                        context.add_observation(
                            "A coding task cannot be completed with an empty patch. Use an editing "
                            "tool to implement the smallest justified change, or state a concrete "
                            "blocker after checking git_diff."
                        )
                        self.event_bus.emit("agent_empty_patch_completion_deferred", iterations=iterations)
                        continue
                    if coding_agent and not mutation_seen:
                        result = AgentResult(
                            "limit_reached", final_text, iterations, self.usage.usage,
                            error="runtime limit reached: no_repository_mutation",
                        )
                        self.event_bus.emit(
                            "agent_loop_finished", status=result.status,
                            error=result.error, duration_seconds=time.monotonic() - started,
                        )
                        return result
                    missing_verification = _missing_required_verification(tool_context)
                    if coding_agent and missing_verification:
                        context.add_observation(
                            "Completion is blocked until supported, revision-current evidence is "
                            "recorded for: " + ", ".join(missing_verification)
                            + ". Use the planned falsification checks and record_evidence; if a "
                            "check is contradicted, revise the implementation."
                        )
                        self.event_bus.emit(
                            "agent_verification_completion_deferred",
                            checks=missing_verification,
                            mutation_revision=int(
                                tool_context.values.get("mutation_revision", 0)
                            ),
                        )
                        continue
                    await self.hooks.run(HookEvent.STOP, final_text=final_text, iterations=iterations)
                    result = AgentResult("completed", final_text, iterations, self.usage.usage)
                    self.event_bus.emit("agent_loop_finished", status=result.status,
                                        duration_seconds=time.monotonic() - started)
                    return result

                for call in response.tool_calls:
                    if self.usage.tool_calls >= self.limits.max_tool_calls:
                        result = ToolResult(False, "Tool-call limit reached", error_code="limit_reached")
                        context.add_tool_result(call, json.dumps(result.to_dict(), ensure_ascii=False))
                        continue
                    if call.name not in active_tool_names:
                        checkpoint_guidance = (
                            " The exploration budget is exhausted; make the smallest justified "
                            "repository change with edit_file/write_file/delete_file."
                            if mutation_checkpoint else ""
                        )
                        if readonly_checkpoint:
                            checkpoint_guidance = (
                                " The read-only exploration budget is exhausted; return the required "
                                "structured final result from existing evidence without another tool."
                            )
                        result = ToolResult(False, f"Tool is not available to agent {agent.name}: {call.name}",
                                            error_code="tool_not_allowed")
                        if checkpoint_guidance:
                            result = ToolResult(
                                False,
                                f"Tool is not available at the implementation checkpoint: "
                                f"{call.name}.{checkpoint_guidance}",
                                error_code="tool_not_allowed",
                            )
                        context.add_tool_result(call, json.dumps(result.to_dict(), ensure_ascii=False))
                        self.usage.record_tool(call.name, False, 0.0)
                        self.event_bus.emit("tool_finished", tool=call.name, call_id=call.id, ok=False,
                                            permission="deny", error_code="tool_not_allowed",
                                            duration_seconds=0.0)
                        continue
                    authorized, decision, reason = await self.permission_manager.authorize(call)
                    self.event_bus.emit("permission_checked", tool=call.name, decision=decision.value, reason=reason)
                    if not authorized:
                        permission = "deny" if decision is PermissionDecision.DENY else "approval_denied"
                        guidance = ""
                        if call.name == "bash" and coding_agent:
                            guidance = (
                                " Use edit_file/write_file/delete_file for repository changes; "
                                "do not invoke shell patching or editor commands."
                            )
                        result = ToolResult(False, f"Permission denied: {reason}.{guidance}",
                                            {"permission": permission}, "permission_denied")
                        context.add_tool_result(call, json.dumps(result.to_dict(), ensure_ascii=False))
                        self.usage.record_tool(call.name, False, 0.0)
                        self.event_bus.emit("tool_finished", tool=call.name, call_id=call.id, ok=False,
                                            permission=permission, duration_seconds=0.0)
                        continue
                    await self.hooks.run(HookEvent.PRE_TOOL_USE, call=call)
                    self.event_bus.emit("tool_called", tool=call.name, call_id=call.id)
                    tool_started = time.monotonic()
                    result = await self.registry.execute(call, tool_context)
                    duration = time.monotonic() - tool_started
                    self.usage.record_tool(call.name, result.ok, duration)
                    self.event_bus.emit("tool_finished", tool=call.name, call_id=call.id,
                                        ok=result.ok, error_code=result.error_code,
                                        duration_seconds=duration, permission="allow")
                    await self.hooks.run(HookEvent.POST_TOOL_USE, call=call, result=result)
                    context.add_tool_result(call, json.dumps(result.to_dict(), ensure_ascii=False))
                    if call.name in validation_tools:
                        successful_test_iteration = iterations if result.ok else None
                    elif result.ok and call.name in mutation_tools:
                        # Any repository mutation invalidates the previous test result.
                        successful_test_iteration = None
                        mutation_seen = True
                context.compact_if_needed()
        except Exception as exc:
            result = AgentResult("failed", final_text, iterations, self.usage.usage,
                                 error=f"{type(exc).__name__}: {exc}")
            self.event_bus.emit("agent_loop_finished", status=result.status, error=result.error,
                                duration_seconds=time.monotonic() - started)
            return result
