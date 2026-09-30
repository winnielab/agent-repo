"""Stage 2 autonomous issue workflow with review, evaluation, and repair."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from repopilot.agents.contracts import extract_normative_contracts
from repopilot.agents.evaluator import EvaluationResult, GoalEvaluator
from repopilot.agents.reviewer import PRReviewAdapter, ReviewInput, ReviewResult
from repopilot.agents.worker import CoderWorker, WorkerResult
from repopilot.config import RepoPilotConfig
from repopilot.core.models import AgentResult, Usage
from repopilot.core.errors import PathBoundaryError, RuntimeLimitError
from repopilot.core.runtime import AgentRuntime, RuntimeLimits
from repopilot.execution.environment import DockerEnvironment, LocalEnvironment
from repopilot.execution.git import run_git
from repopilot.execution.permission import ApprovalCallback, PermissionManager
from repopilot.execution.paths import safe_workspace_path
from repopilot.execution.workspace import Workspace, WorkspaceManager
from repopilot.llm.base import LLMClient
from repopilot.memory.working import WorkingMemory
from repopilot.observability.events import EventBus, JSONLSubscriber
from repopilot.observability.usage import UsageTracker
from repopilot.planning.analyzer import TaskAnalyzer
from repopilot.planning.plan import Plan
from repopilot.planning.planner import Planner
from repopilot.repo.explorer import RepositoryContext, RepositoryExplorer
from repopilot.reporting.issue_report import IssueSolveReport
from repopilot.task.graph import TaskGraph, TaskStatus
from repopilot.task.specification import TaskSpecification
from repopilot.task.intent import requests_test_changes
from repopilot.tools.base import ToolContext
from repopilot.tools.editing import DeleteFileTool, EditFileTool, WriteFileTool
from repopilot.tools.filesystem import GlobTool, ReadFileTool
from repopilot.tools.git import GitDiffTool, GitStatusTool
from repopilot.tools.registry import ToolRegistry
from repopilot.tools.search import SearchCodeTool
from repopilot.tools.shell import BashTool
from repopilot.tools.testing import RunTestsTool


@dataclass
class IssueSolveResult:
    status: str
    run_id: str
    specification: TaskSpecification
    repository: RepositoryContext
    plan: Plan
    workers: List[WorkerResult]
    review: ReviewResult
    evaluation: EvaluationResult
    usage: Usage
    tool_calls: int
    repair_iterations: int
    changed_files: List[str]
    diff: str
    tests: dict
    worktree_path: Optional[Path]
    event_log: Path
    report_path: Path
    diff_path: Optional[Path]
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "resolved"

    @property
    def iterations(self) -> int:
        return sum(worker.iterations for worker in self.workers)


def _metadata_root(repo: Path) -> Path:
    common = Path(run_git(repo, ["rev-parse", "--git-common-dir"]).strip())
    return common if common.is_absolute() else (repo / common).resolve()


def _empty_spec(issue: str) -> TaskSpecification:
    return TaskSpecification(issue[:120] or "Issue", issue or "Issue", ["Issue is resolved"], [])


def _repository_evidence(workspace: Path, context: RepositoryContext,
                         extra_files: Optional[List[str]] = None,
                         char_budget: int = 40_000) -> dict:
    """Capture bounded source contracts selected by the read-only explorer."""
    contents = {}
    contract_lines = {}
    remaining = char_budget
    contract_remaining = 20_000
    files = list(dict.fromkeys([*(extra_files or []), *context.important_files]))
    for relative in files:
        try:
            path = safe_workspace_path(workspace, relative, must_exist=True)
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if remaining > 0:
                text = source[:remaining]
                contents[relative] = text
                remaining -= len(text)
            matches = [
                f"{line_number}: {line.strip()}"
                for line_number, line in enumerate(source.splitlines(), start=1)
                if line.lstrip().startswith("class ") or "__slots__" in line
            ]
            if matches and contract_remaining > 0:
                bounded = "\n".join(matches[:200])[:contract_remaining]
                contract_lines[relative] = bounded.splitlines()
                contract_remaining -= len(bounded)
        except (OSError, ValueError, PathBoundaryError):
            continue
    return {
        "structure_summary": context.structure_summary,
        "target_symbols": context.target_symbols,
        "implementation_files": context.implementation_files,
        "test_files": context.test_files,
        "instruction_files": context.instruction_files,
        "observations": [item.to_dict() for item in context.observations],
        "important_file_contents": contents,
        "contract_lines": contract_lines,
        "normative_contracts": extract_normative_contracts(contents),
    }


def _plan_referenced_files(plan: Plan) -> List[str]:
    text = "\n".join(
        [plan.summary]
        + [f"{step.title}\n{step.description}" for step in plan.steps]
    )
    candidates = re.findall(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+", text)
    return list(dict.fromkeys(candidates))


def _worker_token_ceiling(config: RepoPilotConfig, *, repair: bool = False) -> int:
    reserve = config.finalization_token_reserve
    if not repair and config.max_repair_iterations:
        reserve += config.repair_token_reserve + config.evaluation_token_reserve
    reserve = min(reserve, max(0, config.max_tokens - 1))
    return max(1, config.max_tokens - reserve)


def _worker_runtime_ceiling(config: RepoPilotConfig, remaining_seconds: float) -> float:
    """Reserve enough wall time for one configured final test command.

    The reserve is capped at half of the whole workflow so small local runs can
    still make progress, while long benchmark runs preserve their full command
    timeout instead of discovering a shrunken deadline only at finalization.
    """
    reserve = min(config.command_timeout_seconds, config.max_runtime_seconds / 2)
    return max(0.001, remaining_seconds - reserve)


async def solve_issue(
    repository: Path,
    issue: str,
    llm: LLMClient,
    config: Optional[RepoPilotConfig] = None,
    approval_callback: Optional[ApprovalCallback] = None,
    reviewer: Optional[PRReviewAdapter] = None,
    run_id: Optional[str] = None,
) -> IssueSolveResult:
    config = config or RepoPilotConfig()
    config.validate()
    if not issue.strip():
        raise ValueError("issue cannot be blank")
    repository = Path(repository).resolve()
    run_id = run_id or uuid4().hex
    root = _metadata_root(repository) / "repopilot"
    event_log = root / "runs" / f"{run_id}.jsonl"
    artifact_dir = root / "issues" / run_id
    bus = EventBus(run_id, [JSONLSubscriber(event_log)])
    tracker = UsageTracker()
    started = time.monotonic()
    workspace: Optional[Workspace] = None
    specification = _empty_spec(issue)
    repository_context = RepositoryContext("unknown")
    plan = Plan("Workflow did not reach planning", [])
    graph: Optional[TaskGraph] = None
    workers: List[WorkerResult] = []
    review = ReviewResult("reject", raw_artifacts={"summary": "Review did not run"})
    evaluation = EvaluationResult(False, summary="Evaluation did not run")
    diff, tests, changed_files = "", {}, []
    diff_path: Optional[Path] = None
    repairs = 0
    error: Optional[str] = None
    bus.emit("issue_run_started", issue=issue, source_repo=str(repository))

    def limit_reason() -> Optional[str]:
        if time.monotonic() - started >= config.max_runtime_seconds:
            return "max_runtime_seconds"
        if tracker.usage.total_tokens >= config.max_tokens:
            return "max_tokens"
        if (config.max_cost is not None and tracker.usage.cost is not None
                and tracker.usage.cost >= config.max_cost):
            return "max_cost"
        return None

    def enforce_budget() -> float:
        reason = limit_reason()
        if reason:
            bus.emit("limit_reached", limit=reason, scope="issue_workflow")
            raise RuntimeLimitError(f"issue workflow limit reached: {reason}")
        return max(0.001, config.max_runtime_seconds - (time.monotonic() - started))

    def worker_token_ceiling(*, repair: bool = False) -> int:
        return _worker_token_ceiling(config, repair=repair)

    def capture_workspace() -> None:
        nonlocal diff, changed_files, diff_path
        if workspace is None:
            return
        diff = workspace.collect_diff()
        changed_files = workspace.changed_files()
        if diff:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            diff_path = artifact_dir / "changes.patch"
            diff_path.write_text(diff, encoding="utf-8")

    async def bounded(awaitable):
        try:
            timeout = enforce_budget()
        except Exception:
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            bus.emit("limit_reached", limit="max_runtime_seconds", scope="issue_workflow")
            raise RuntimeLimitError("issue workflow limit reached: max_runtime_seconds") from exc

    try:
        workspace = WorkspaceManager(
            config.worktree_parent,
            bus,
            hydrate_runtime_artifacts=config.hydrate_runtime_artifacts,
            max_runtime_artifact_files=config.max_runtime_artifact_files,
            max_runtime_artifact_bytes=config.max_runtime_artifact_bytes,
        ).create(repository, run_id)
        host_environment = LocalEnvironment(workspace.path, config.command_timeout_seconds,
                                            config.max_command_output_chars)
        environment = (
            DockerEnvironment(
                workspace.path, config.execution_image,
                platform=config.execution_platform,
                setup_command=config.execution_setup_command,
                default_timeout=config.command_timeout_seconds,
                max_output_chars=config.max_command_output_chars,
            )
            if config.execution_image else host_environment
        )
        test_tool = RunTestsTool(config.test_command, config.command_timeout_seconds,
                                 environment=host_environment)
        readonly_registry = ToolRegistry([
            GlobTool(), ReadFileTool(), SearchCodeTool(), GitStatusTool(),
        ])
        registry = ToolRegistry([
            GlobTool(), ReadFileTool(), SearchCodeTool(), WriteFileTool(), EditFileTool(),
            DeleteFileTool(), BashTool(), test_tool, GitStatusTool(), GitDiffTool(),
        ])
        permission = PermissionManager(workspace.path, config.allowed_command_prefixes, approval_callback)
        tool_context = ToolContext(workspace.path, environment, run_id, bus)
        test_requirement = config.test_command
        if config.baseline_aware_tests:
            baseline = await bounded(test_tool.establish_baseline(tool_context))
            if not baseline.ok:
                failure_ids = sorted(test_tool.failure_ids(baseline))
                test_requirement += (
                    "\nBaseline note: this command has recognized pre-existing failures. "
                    "The patch must introduce no new failure identifiers; existing baseline "
                    f"failures are tolerated: {failure_ids[:20]}"
                )
        specification = await bounded(TaskAnalyzer(llm, bus, tracker).analyze(issue, test_requirement))
        tool_context.values["allow_test_changes"] = requests_test_changes(specification)
        remaining = enforce_budget()
        explorer_token_ceiling = min(
            worker_token_ceiling(),
            tracker.usage.total_tokens + config.max_explorer_tokens,
        )
        limits = RuntimeLimits(
            config.max_explorer_iterations + 2,
            config.max_explorer_tool_calls,
            explorer_token_ceiling,
            remaining,
            config.max_post_test_iterations,
            0,
            config.max_explorer_iterations,
        )
        explorer_runtime = AgentRuntime(llm, readonly_registry, permission, bus,
                                        usage=tracker, limits=limits)
        repository_context = await bounded(RepositoryExplorer(
            explorer_runtime, tool_context, artifact_dir / "explorer", config.context_char_budget,
        ).inspect(config.test_command, specification))
        repository_evidence = _repository_evidence(workspace.path, repository_context)
        plan = await bounded(Planner(llm, bus, tracker).create_plan(
            specification, repository_context, repository_evidence,
        ))
        plan_files = _plan_referenced_files(plan)
        repository_evidence = _repository_evidence(
            workspace.path, repository_context, extra_files=plan_files,
        )
        graph = TaskGraph.from_plan(plan, artifact_dir / "tasks")
        memory = WorkingMemory.load(artifact_dir / "working-memory.json")
        for path in repository_context.important_files:
            memory.remember("relevant_files", path)

        limits = RuntimeLimits(
            config.max_iterations, config.max_tool_calls, worker_token_ceiling(),
            _worker_runtime_ceiling(config, enforce_budget()),
            config.max_post_test_iterations, config.max_no_mutation_iterations,
        )
        coder_runtime = AgentRuntime(llm, registry, permission, bus, usage=tracker, limits=limits)
        worker = CoderWorker(coder_runtime, tool_context, artifact_dir / "workers",
                             config.context_char_budget, config.large_tool_result_chars)
        while graph.ready_tasks():
            record = graph.ready_tasks()[0]
            graph.mark_running(record.id)
            worker.runtime.limits = RuntimeLimits(
                config.max_iterations, config.max_tool_calls, worker_token_ceiling(),
                _worker_runtime_ceiling(config, enforce_budget()),
                config.max_post_test_iterations,
                config.max_no_mutation_iterations,
            )
            worker_result, agent_result = await bounded(worker.run(
                record,
                specification,
                memory,
                plan=plan,
                repository_evidence=repository_evidence,
            ))
            workers.append(worker_result)
            if agent_result.success:
                graph.mark_completed(record.id)
            else:
                graph.mark_failed(record.id, agent_result.error or agent_result.status)
                break

        remaining_for_test = enforce_budget()
        effective_test_timeout = min(config.command_timeout_seconds, remaining_for_test)
        timeout_reason = (
            "the issue workflow deadline had less time remaining"
            if effective_test_timeout < config.command_timeout_seconds
            else "configured"
        )
        final_test = await bounded(test_tool.execute(
            {}, tool_context,
            timeout=effective_test_timeout,
            timeout_reason=timeout_reason,
        ))
        tests = dict(final_test.metadata)
        tests.update(ok=final_test.ok, output=final_test.content)
        if not final_test.ok:
            memory.remember("test_failures", final_test.content[-4000:])
        capture_workspace()
        repository_evidence = _repository_evidence(
            workspace.path, repository_context,
            extra_files=[
                *changed_files,
                *tool_context.values.get("read_files", []),
                *plan_files,
            ],
        )
        repository_evidence["command_evidence"] = tool_context.values.get("command_evidence", [])
        for path in changed_files:
            memory.remember("relevant_files", path)
        reviewer = reviewer or PRReviewAdapter(artifact_dir / "review", mode="simple")
        review_input = ReviewInput(
            specification, plan, workspace.path, diff, tests, config.test_command,
            config.command_timeout_seconds,
        )
        review = await bounded(asyncio.to_thread(reviewer.review, review_input))
        bus.emit("review_finished", decision=review.decision, issues=len(review.issues))
        evaluator = GoalEvaluator(llm, bus, tracker)
        evaluation = await bounded(evaluator.evaluate(
            specification, diff, tests, review, repository_evidence, plan,
        ))

        while not evaluation.passed and repairs < config.max_repair_iterations:
            enforce_budget()
            repairs += 1
            feedback = evaluation.repair_feedback()
            memory.remember("failed_attempts", f"Repair {repairs}: {feedback}")
            for criterion in evaluation.criteria:
                if criterion.status != "pass" and criterion.invalidated_assumption:
                    memory.remember(
                        "invalidated_assumptions",
                        criterion.invalidated_assumption,
                    )
            dependencies = [item.id for item in graph.list() if item.status is TaskStatus.COMPLETED]
            repair_task = graph.add(f"Repair pass {repairs}", feedback or "Address evaluator feedback", dependencies)
            graph.mark_running(repair_task.id)
            worker.runtime.limits = RuntimeLimits(
                config.max_iterations, config.max_tool_calls, worker_token_ceiling(repair=True),
                _worker_runtime_ceiling(config, enforce_budget()),
                config.max_post_test_iterations,
                config.max_no_mutation_iterations,
            )
            worker_result, agent_result = await bounded(worker.run(
                repair_task,
                specification,
                memory,
                feedback,
                plan,
                repository_evidence,
            ))
            workers.append(worker_result)
            if not agent_result.success:
                graph.mark_failed(repair_task.id, agent_result.error or agent_result.status)
                break
            graph.mark_completed(repair_task.id)
            remaining_for_test = enforce_budget()
            effective_test_timeout = min(config.command_timeout_seconds, remaining_for_test)
            timeout_reason = (
                "the issue workflow deadline had less time remaining"
                if effective_test_timeout < config.command_timeout_seconds
                else "configured"
            )
            final_test = await bounded(test_tool.execute(
                {}, tool_context,
                timeout=effective_test_timeout,
                timeout_reason=timeout_reason,
            ))
            tests = dict(final_test.metadata)
            tests.update(ok=final_test.ok, output=final_test.content)
            capture_workspace()
            repository_evidence = _repository_evidence(
                workspace.path, repository_context,
                extra_files=[
                    *changed_files,
                    *tool_context.values.get("read_files", []),
                    *plan_files,
                ],
            )
            repository_evidence["command_evidence"] = tool_context.values.get("command_evidence", [])
            review_input = ReviewInput(
                specification, plan, workspace.path, diff, tests, config.test_command,
                config.command_timeout_seconds,
            )
            review = await bounded(asyncio.to_thread(reviewer.review, review_input))
            bus.emit("review_finished", decision=review.decision, issues=len(review.issues), repair=repairs)
            evaluation = await bounded(evaluator.evaluate(
                specification, diff, tests, review, repository_evidence, plan,
            ))

        status = "resolved" if evaluation.passed else "failed"
        if status == "failed":
            error = evaluation.repair_feedback() or evaluation.summary or "goal was not satisfied"
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        try:
            capture_workspace()
            if diff:
                bus.emit("partial_result_saved", changed_files=changed_files,
                         diff_path=str(diff_path), error=error)
        except Exception as capture_exc:
            bus.emit("partial_result_save_failed", error=f"{type(capture_exc).__name__}: {capture_exc}")

    elapsed = time.monotonic() - started
    bus.emit("issue_run_finished", status=status, repairs=repairs, error=error,
             duration_seconds=elapsed, changed_files=changed_files)
    task_rows = [record.to_dict() for record in graph.list()] if graph else []
    report = IssueSolveReport(
        status, run_id, issue, specification.to_dict(), repository_context.to_dict(), plan.to_dict(),
        task_rows, [item.to_dict() for item in workers], tests, review.to_dict(), evaluation.to_dict(),
        bool(tests.get("ok", False)), bool(tests.get("ok", False)),
        repairs, sum(item.iterations for item in workers), tracker.tool_calls,
        tracker.usage.input_tokens, tracker.usage.output_tokens,
        tracker.usage.cost, elapsed, changed_files, str(diff_path) if diff_path else None,
        str(event_log), str(workspace.path) if workspace else None, error,
    )
    report_path, _ = report.write(artifact_dir)
    if workspace and not config.keep_worktree:
        try:
            workspace.collect_diff()
            workspace.cleanup()
        except Exception as exc:
            bus.emit("workspace_cleanup_finished", workspace=str(workspace.path), error=str(exc))
    return IssueSolveResult(status, run_id, specification, repository_context, plan, workers,
                            review, evaluation, tracker.usage, tracker.tool_calls, repairs,
                            changed_files, diff, tests, workspace.path if workspace else None,
                            event_log, report_path, diff_path, error)
