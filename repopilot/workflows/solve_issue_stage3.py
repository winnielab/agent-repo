"""Stage 3 issue workflow: isolated worker waves, atomic merge, and verification."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from repopilot.agents.evaluator import EvaluationResult, GoalEvaluator
from repopilot.agents.contracts import patch_hygiene_violations
from repopilot.agents.reviewer import PRReviewAdapter, ReviewInput, ReviewResult
from repopilot.agents.worker import CoderWorker, WorkerResult
from repopilot.config import RepoPilotConfig
from repopilot.core.errors import RuntimeLimitError
from repopilot.core.models import Usage
from repopilot.core.runtime import AgentRuntime, RuntimeLimits
from repopilot.execution.environment import DockerEnvironment, LocalEnvironment
from repopilot.execution.git import run_git
from repopilot.execution.permission import ApprovalCallback, PermissionManager
from repopilot.execution.workspace import Workspace, WorkspaceManager
from repopilot.llm.base import LLMClient
from repopilot.memory.working import WorkingMemory
from repopilot.observability.events import EventBus, JSONLSubscriber
from repopilot.observability.usage import UsageTracker
from repopilot.orchestration import (
    AgentCoordinator,
    MergeManager,
    PatchArtifact,
    TaskWorkspaceProvider,
)
from repopilot.planning.analyzer import TaskAnalyzer
from repopilot.planning.plan import Plan
from repopilot.planning.planner import Planner
from repopilot.repo.explorer import RepositoryContext, RepositoryExplorer
from repopilot.reporting.issue_report import IssueSolveReport
from repopilot.task.graph import TaskGraph, TaskRecord, TaskStatus
from repopilot.task.intent import requests_test_changes
from repopilot.tools.base import ToolContext
from repopilot.tools.editing import DeleteFileTool, EditFileTool, WriteFileTool
from repopilot.tools.evidence import RecordEvidenceTool
from repopilot.tools.filesystem import GlobTool, ReadFileTool
from repopilot.tools.git import GitDiffTool, GitStatusTool
from repopilot.tools.registry import ToolRegistry
from repopilot.tools.search import SearchCodeTool
from repopilot.tools.shell import BashTool
from repopilot.tools.testing import RunTestsTool
from repopilot.workflows.solve_issue import (
    IssueSolveResult,
    _empty_spec,
    _metadata_root,
    _plan_referenced_files,
    _repository_evidence,
)


def _commit_wave(workspace: Path, message: str) -> str:
    run_git(workspace, ["add", "-A"])
    run_git(workspace, [
        "-c", "user.name=RepoPilot",
        "-c", "user.email=repopilot@example.invalid",
        "commit", "-m", message,
    ])
    return run_git(workspace, ["rev-parse", "HEAD"]).strip()


def _diff_from_base(workspace: Workspace, base_commit: str) -> tuple[str, List[str]]:
    committed = run_git(
        workspace.path, ["diff", "--binary", "--no-ext-diff", base_commit, "HEAD"],
    )
    working = workspace.collect_diff()
    chunks = [item.rstrip("\n") for item in (committed, working) if item.strip()]
    diff = "\n".join(chunks) + ("\n" if chunks else "")
    committed_files = run_git(
        workspace.path, ["diff", "--name-only", "-z", base_commit, "HEAD"],
    )
    changed = set(filter(None, committed_files.split("\0")))
    changed.update(workspace.changed_files())
    return diff, sorted(changed)


def _explorer_token_ceiling(config: RepoPilotConfig, used_tokens: int) -> int:
    """Bound exploration while preserving budget for planning and implementation."""
    fixed_reserve = config.finalization_token_reserve + config.evaluation_token_reserve
    if config.max_repair_iterations:
        fixed_reserve += config.repair_token_reserve
    phase_budget = max(1, config.max_tokens - used_tokens - fixed_reserve)
    # Planning still follows exploration and the number of TaskGraph nodes is
    # not known yet. Give exploration at most one third of the unreserved pool
    # so default-sized Stage 3 runs cannot starve every coding worker.
    explorer_allowance = min(config.max_explorer_tokens, max(1, phase_budget // 3))
    return min(config.max_tokens, used_tokens + explorer_allowance)


async def solve_issue_stage3(
    repository: Path,
    issue: str,
    llm: LLMClient,
    config: Optional[RepoPilotConfig] = None,
    approval_callback: Optional[ApprovalCallback] = None,
    reviewer: Optional[PRReviewAdapter] = None,
    run_id: Optional[str] = None,
) -> IssueSolveResult:
    """Solve an issue through coordinator-managed, task-isolated worker waves.

    Read-only analysis is shared once. Every coding assignment receives its own
    worktree, runtime, tool context, usage tracker, memory, and event log. A
    frontier is merged atomically into the issue worktree and verified before
    dependent tasks are released. Patches are persisted even when a worker
    reaches a runtime limit after mutating its workspace.
    """
    config = config or RepoPilotConfig()
    config.validate()
    if not issue.strip():
        raise ValueError("issue cannot be blank")
    repository = Path(repository).resolve()
    run_id = run_id or uuid4().hex
    root = _metadata_root(repository) / "repopilot"
    event_log = root / "runs" / f"{run_id}.jsonl"
    artifact_dir = root / "issues" / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus(run_id, [JSONLSubscriber(event_log)])
    tracker = UsageTracker()
    started = time.monotonic()
    workspace: Optional[Workspace] = None
    base_commit = ""
    specification = _empty_spec(issue)
    repository_context = RepositoryContext("unknown")
    plan = Plan("Workflow did not reach planning", [])
    graph: Optional[TaskGraph] = None
    workers: List[WorkerResult] = []
    review = ReviewResult("reject", raw_artifacts={"summary": "Review did not run"})
    evaluation = EvaluationResult(False, summary="Evaluation did not run")
    diff = ""
    changed_files: List[str] = []
    tests: Dict[str, object] = {}
    diff_path: Optional[Path] = None
    repairs = 0
    error: Optional[str] = None
    worker_tool_calls = 0
    wave_count = 0
    max_parallelism = 0
    conflict_repairs = 0
    hygiene_repairs = 0
    patch_artifacts: Dict[str, PatchArtifact] = {}
    feedback_by_task: Dict[str, str] = {}
    baseline_result = None
    root_tool_context: Optional[ToolContext] = None

    bus.emit(
        "stage3_issue_run_started", issue=issue, source_repo=str(repository),
        max_concurrency=config.max_concurrency,
    )

    def remaining_seconds() -> float:
        remaining = config.max_runtime_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise RuntimeLimitError("stage3 workflow limit reached: max_runtime_seconds")
        return remaining

    async def bounded(awaitable):
        try:
            return await asyncio.wait_for(awaitable, timeout=max(0.001, remaining_seconds()))
        except asyncio.TimeoutError as exc:
            raise RuntimeLimitError(
                "stage3 workflow limit reached: max_runtime_seconds"
            ) from exc

    def capture_final() -> None:
        nonlocal diff, changed_files, diff_path
        if workspace is None or not base_commit:
            return
        diff, changed_files = _diff_from_base(workspace, base_commit)
        if diff:
            diff_path = artifact_dir / "changes.patch"
            temporary = diff_path.with_suffix(".patch.tmp")
            temporary.write_text(diff, encoding="utf-8")
            temporary.replace(diff_path)

    try:
        workspace = WorkspaceManager(
            config.worktree_parent,
            bus,
            hydrate_runtime_artifacts=config.hydrate_runtime_artifacts,
            max_runtime_artifact_files=config.max_runtime_artifact_files,
            max_runtime_artifact_bytes=config.max_runtime_artifact_bytes,
        ).create(repository, run_id)
        base_commit = run_git(workspace.path, ["rev-parse", "HEAD"]).strip()
        manifest = {
            "schema_version": 1,
            "mode": "stage3-coordinator",
            "run_id": run_id,
            "source_repository": str(repository),
            "base_commit": base_commit,
            "max_concurrency": config.max_concurrency,
            "max_tokens": config.max_tokens,
            "max_worker_tokens": config.max_worker_tokens,
        }
        (artifact_dir / "stage3-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )

        host_environment = LocalEnvironment(
            workspace.path, config.command_timeout_seconds, config.max_command_output_chars,
        )
        root_environment = (
            DockerEnvironment(
                workspace.path, config.execution_image,
                platform=config.execution_platform,
                setup_command=config.execution_setup_command,
                default_timeout=config.command_timeout_seconds,
                max_output_chars=config.max_command_output_chars,
            )
            if config.execution_image else host_environment
        )
        root_test_tool = RunTestsTool(
            config.test_command, config.command_timeout_seconds,
            environment=host_environment,
        )
        readonly_registry = ToolRegistry([
            GlobTool(), ReadFileTool(), SearchCodeTool(), GitStatusTool(),
        ])
        root_permission = PermissionManager(
            workspace.path, config.allowed_command_prefixes, approval_callback,
        )
        root_tool_context = ToolContext(workspace.path, root_environment, run_id, bus)
        test_requirement = config.test_command
        if config.baseline_aware_tests:
            baseline_result = await bounded(root_test_tool.establish_baseline(root_tool_context))
            if not baseline_result.ok:
                failure_ids = sorted(root_test_tool.failure_ids(baseline_result))
                test_requirement += (
                    "\nBaseline note: this command has recognized pre-existing failures. "
                    "The patch must introduce no new failure identifiers; existing baseline "
                    f"failures are tolerated: {failure_ids[:20]}"
                )

        specification = await bounded(
            TaskAnalyzer(llm, bus, tracker).analyze(issue, test_requirement)
        )
        root_tool_context.values["allow_test_changes"] = requests_test_changes(specification)
        explorer_limit = _explorer_token_ceiling(
            config, tracker.usage.total_tokens,
        )
        explorer_runtime = AgentRuntime(
            llm, readonly_registry, root_permission, bus, usage=tracker,
            limits=RuntimeLimits(
                config.max_explorer_iterations + 2,
                config.max_explorer_tool_calls,
                explorer_limit,
                remaining_seconds(),
                config.max_post_test_iterations,
                0,
                config.max_explorer_iterations,
            ),
        )
        repository_context = await bounded(RepositoryExplorer(
            explorer_runtime,
            root_tool_context,
            artifact_dir / "explorer",
            config.context_char_budget,
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
        workspace_provider = TaskWorkspaceProvider(
            workspace.path,
            graph,
            run_id,
            workspace.path.parent / ".repopilot-task-worktrees",
            artifact_dir / "patches",
        )

        def worker_budget(frontier_size: int, *, repair: bool = False) -> int:
            reserve = config.finalization_token_reserve + config.evaluation_token_reserve
            if not repair and config.max_repair_iterations:
                reserve += config.repair_token_reserve
            available = max(1, config.max_tokens - tracker.usage.total_tokens - reserve)
            # Every pending task will eventually run, even when only
            # max_concurrency of them run at once. Divide by the complete
            # remaining set so queued/dependent workers cannot collectively
            # overspend the run or leave the final dependency with one token.
            tasks = max(1, frontier_size)
            return max(1, min(config.max_worker_tokens, available // tasks))

        current_worker_budget = worker_budget(max(1, len(graph.ready_tasks())))

        async def run_worker(record: TaskRecord, assignment) -> WorkerResult:
            lease = workspace_provider.acquire(
                record, assignment.lease_owner,
                agent_session_id=assignment.run_id + ":" + assignment.worker_id,
            )
            worker_bus = EventBus(
                assignment.run_id + ":" + assignment.worker_id,
                [JSONLSubscriber(
                    artifact_dir / "workers" / record.id / f"attempt-{assignment.attempt}.jsonl"
                )],
            )
            local_tracker = UsageTracker()
            local_memory = WorkingMemory.load(
                artifact_dir / "workers" / record.id /
                f"attempt-{assignment.attempt}-memory.json"
            )
            for path in repository_context.important_files:
                local_memory.remember("relevant_files", path)
            local_host = LocalEnvironment(
                lease.workspace.path,
                config.command_timeout_seconds,
                config.max_command_output_chars,
            )
            local_environment = (
                DockerEnvironment(
                    lease.workspace.path, config.execution_image,
                    platform=config.execution_platform,
                    setup_command=config.execution_setup_command,
                    default_timeout=config.command_timeout_seconds,
                    max_output_chars=config.max_command_output_chars,
                )
                if config.execution_image else local_host
            )
            local_test = RunTestsTool(
                config.test_command,
                config.command_timeout_seconds,
                environment=local_host,
            )
            local_test.baseline_result = baseline_result
            registry = ToolRegistry([
                GlobTool(), ReadFileTool(), SearchCodeTool(), WriteFileTool(),
                EditFileTool(), DeleteFileTool(), BashTool(), RecordEvidenceTool(),
                local_test, GitStatusTool(), GitDiffTool(),
            ])
            permission = PermissionManager(
                lease.workspace.path,
                config.allowed_command_prefixes,
                approval_callback,
            )
            tool_context = ToolContext(
                lease.workspace.path, local_environment,
                assignment.run_id + ":" + assignment.worker_id,
                worker_bus,
            )
            tool_context.values["allow_test_changes"] = requests_test_changes(specification)
            tool_context.values["hypothesis_check_keys"] = [
                item.key for item in plan.hypothesis_checks
            ]
            tool_context.values["hypothesis_check_kinds"] = {
                item.key: item.kind for item in plan.hypothesis_checks
            }
            tool_context.values["required_verification_keys"] = [
                item.key for item in plan.hypothesis_checks
                if item.kind == "candidate_semantics"
            ]
            runtime = AgentRuntime(
                llm,
                registry,
                permission,
                worker_bus,
                usage=local_tracker,
                limits=RuntimeLimits(
                    config.max_iterations,
                    config.max_tool_calls,
                    current_worker_budget,
                    max(1.0, remaining_seconds() - min(
                        config.command_timeout_seconds,
                        config.max_runtime_seconds / 3,
                    )),
                    config.max_post_test_iterations,
                    config.max_no_mutation_iterations,
                ),
            )
            coder = CoderWorker(
                runtime,
                tool_context,
                artifact_dir / "workers" / record.id / f"attempt-{assignment.attempt}",
                config.context_char_budget,
                config.large_tool_result_chars,
            )
            agent_result = None
            patch_path = ""
            patch_sha256 = ""
            changed: tuple[str, ...] = ()
            try:
                _worker, agent_result = await coder.run(
                    record,
                    specification,
                    local_memory,
                    feedback_by_task.get(record.id, ""),
                    plan,
                    repository_evidence,
                )
                changed = tuple(lease.workspace.changed_files())
                if changed:
                    path, patch_sha256 = lease.persist_patch()
                    patch_path = str(path)
                    status = "produced"
                    message = agent_result.error or ""
                else:
                    lease.workspace.collect_diff()
                    status = agent_result.status
                    message = agent_result.error or "worker produced an empty patch"
                usage = local_tracker.usage
                return WorkerResult(
                    record.id,
                    status,
                    agent_result.final_text,
                    agent_result.iterations,
                    message,
                    lease.workspace_id,
                    lease.base_commit,
                    patch_path,
                    patch_sha256,
                    "",
                    changed,
                    local_test.last_result.to_dict() if local_test.last_result else None,
                    assignment.run_id + ":" + assignment.worker_id,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                    usage.cost,
                    local_tracker.tool_calls,
                    tuple(tool_context.values.get("command_evidence", [])),
                    tuple(tool_context.values.get("verification_evidence", [])),
                )
            finally:
                # collect_diff is idempotent and authorizes safe cleanup even on
                # exceptions; persisted patches remain in the issue artifact dir.
                try:
                    lease.workspace.collect_diff()
                    lease.workspace.cleanup()
                except Exception as cleanup_exc:
                    worker_bus.emit("worker_workspace_cleanup_failed", error=str(cleanup_exc))

        async def verify_target(path: Path) -> Dict[str, object]:
            test_tool = RunTestsTool(
                config.test_command,
                config.command_timeout_seconds,
                environment=LocalEnvironment(
                    path, config.command_timeout_seconds, config.max_command_output_chars,
                ),
            )
            test_tool.baseline_result = baseline_result
            environment = (
                DockerEnvironment(
                    path, config.execution_image,
                    platform=config.execution_platform,
                    setup_command=config.execution_setup_command,
                    default_timeout=config.command_timeout_seconds,
                    max_output_chars=config.max_command_output_chars,
                )
                if config.execution_image else LocalEnvironment(path)
            )
            context = ToolContext(path, environment, run_id, bus)
            result = await test_tool.execute({}, context)
            payload = dict(result.metadata)
            payload.update(ok=result.ok, output=result.content)
            return payload

        async def execute_ready_wave(*, repair: bool = False) -> List[WorkerResult]:
            nonlocal current_worker_budget, worker_tool_calls, wave_count, max_parallelism
            nonlocal tests, conflict_repairs, hygiene_repairs
            ready = graph.ready_tasks()
            if not ready:
                return []
            remaining_tasks = (
                len(ready)
                if repair
                else sum(
                    item.status in {TaskStatus.PENDING, TaskStatus.RETRYABLE}
                    for item in graph.list()
                )
            )
            current_worker_budget = worker_budget(remaining_tasks, repair=repair)
            wave_count += 1
            bus.emit(
                "stage3_wave_started", wave=wave_count,
                ready_tasks=[item.id for item in ready],
                worker_token_budget=current_worker_budget,
            )
            coordinator = AgentCoordinator(
                run_worker,
                max_concurrency=config.max_concurrency,
                lease_seconds=max(60.0, min(config.max_runtime_seconds, 600.0)),
                run_id=f"{run_id}:wave-{wave_count}",
                event_bus=bus,
            )
            outcome = await bounded(coordinator.execute(graph))
            max_parallelism = max(max_parallelism, outcome.max_parallelism)
            for item in outcome.results:
                workers.append(item)
                tracker.add_usage(Usage(
                    item.input_tokens, item.output_tokens, item.cached_tokens, item.cost,
                ))
                worker_tool_calls += item.tool_calls
                if item.patch_path:
                    artifact = PatchArtifact(
                        item.task_id,
                        Path(item.patch_path),
                        item.patch_sha256,
                        item.base_commit,
                        item.result_commit or None,
                    )
                    patch_artifacts[item.task_id] = artifact
            produced = [
                patch_artifacts[item.task_id]
                for item in outcome.results if item.task_id in patch_artifacts
            ]
            if not produced:
                bus.emit("stage3_wave_finished", wave=wave_count, status="no_patch")
                return outcome.results
            mergeable: List[PatchArtifact] = []
            rejected: List[tuple[PatchArtifact, List[Dict[str, object]]]] = []
            requested_text = "\n".join([
                specification.title,
                specification.description,
                *specification.constraints,
            ])
            for artifact in produced:
                artifact_diff = artifact.patch_path.read_text(
                    encoding="utf-8", errors="replace",
                )
                violations = patch_hygiene_violations(
                    artifact_diff,
                    allow_test_changes=requests_test_changes(specification),
                    requested_text=requested_text,
                )
                if violations:
                    rejected.append((artifact, violations))
                    graph.mark_failed(
                        artifact.task_id,
                        "patch hygiene rejected: " + "; ".join(
                            f"{item['kind']}:{item['file']}" for item in violations
                        ),
                    )
                    bus.emit(
                        "stage3_patch_hygiene_rejected",
                        task_id=artifact.task_id,
                        violations=violations,
                    )
                else:
                    mergeable.append(artifact)
            if rejected and hygiene_repairs < 1:
                hygiene_repairs += 1
                remaining_patch_chars = 36_000
                rejected_context = []
                for artifact, violations in rejected:
                    if remaining_patch_chars <= 0:
                        break
                    artifact_diff = artifact.patch_path.read_text(
                        encoding="utf-8", errors="replace",
                    )
                    bounded_patch = artifact_diff[:remaining_patch_chars]
                    remaining_patch_chars -= len(bounded_patch)
                    rejected_context.append(
                        f"### Rejected candidate {artifact.task_id}\n"
                        f"Violations: {violations}\n{bounded_patch}"
                    )
                hygiene_feedback = (
                    "The prior candidate was rejected before merge because it included temporary, "
                    "scratch, or unrequested test artifacts. Starting from the current clean base, "
                    "implement the production fix only. Do not recreate probe/scratch files and "
                    "do not edit tests unless the issue explicitly requests tests.\n\n"
                    + "\n\n".join(rejected_context)
                )
                hygiene_task = graph.add(
                    f"Consolidate wave {wave_count} without temporary artifacts",
                    hygiene_feedback,
                    [],
                )
                feedback_by_task[hygiene_task.id] = hygiene_feedback
                bus.emit(
                    "stage3_hygiene_repair_scheduled", wave=wave_count,
                    task_id=hygiene_task.id,
                    rejected_tasks=[item.task_id for item, _ in rejected],
                )
            produced = mergeable
            if not produced:
                bus.emit(
                    "stage3_wave_finished", wave=wave_count,
                    status="patch_hygiene_rejected",
                )
                return outcome.results
            for artifact in produced:
                graph.mark_merging(artifact.task_id)
            merge = await bounded(MergeManager(
                workspace.path, event_bus=bus, verifier=verify_target,
            ).merge(produced))
            tests = dict(merge.tests)
            applied = bool(merge.changed_files)
            commit: Optional[str] = None
            if applied:
                commit = _commit_wave(
                    workspace.path,
                    f"RepoPilot Stage 3 wave {wave_count}: "
                    + ", ".join(item.task_id for item in produced),
                )
            for artifact in produced:
                if merge.success:
                    graph.mark_verifying(artifact.task_id, result_commit=commit)
                    graph.mark_completed(artifact.task_id)
                else:
                    if applied and graph.get(artifact.task_id).status is TaskStatus.MERGING:
                        graph.mark_verifying(artifact.task_id, result_commit=commit)
                    graph.mark_failed(
                        artifact.task_id,
                        merge.error or merge.status,
                    )
            if (merge.classification == "same_file_conflict"
                    and conflict_repairs < 1):
                conflict_repairs += 1
                patch_context = []
                remaining_patch_chars = 36_000
                for artifact in produced:
                    if remaining_patch_chars <= 0:
                        break
                    text = artifact.patch_path.read_text(
                        encoding="utf-8", errors="replace",
                    )
                    bounded_patch = text[:remaining_patch_chars]
                    remaining_patch_chars -= len(bounded_patch)
                    patch_context.append(
                        f"### Candidate from {artifact.task_id}\n{bounded_patch}"
                    )
                feedback = (
                    "The parallel wave produced overlapping patches and none were applied. "
                    "Create one integrated production-code patch from the clean current base that "
                    "satisfies all plan behaviors. Reconcile the candidate intent; do not add "
                    "scratch/probe files, do not merely copy conflict markers, and run focused "
                    "verification after editing.\n\n"
                    + "\n\n".join(patch_context)
                )
                consolidation = graph.add(
                    f"Consolidate wave {wave_count} conflicts",
                    feedback,
                    [],
                )
                feedback_by_task[consolidation.id] = feedback
                bus.emit(
                    "stage3_conflict_repair_scheduled", wave=wave_count,
                    task_id=consolidation.id, conflicts=merge.conflicts,
                )
            capture_final()
            bus.emit(
                "stage3_wave_finished", wave=wave_count, status=merge.status,
                classification=merge.classification,
                merged_tasks=merge.merged_tasks,
            )
            return outcome.results

        while graph.ready_tasks():
            ready = graph.ready_tasks()
            is_repair_wave = bool(ready) and all(
                item.id in feedback_by_task for item in ready
            )
            wave_results = await execute_ready_wave(repair=is_repair_wave)
            if not wave_results or not any(item.patch_path for item in wave_results):
                break

        def attach_worker_evidence(evidence: Dict[str, object]) -> None:
            merged_workers = [
                item for item in workers
                if graph.get(item.task_id).result_commit is not None
            ]
            evidence["command_evidence"] = [
                item for worker in merged_workers for item in worker.command_evidence
            ]
            latest_checks: Dict[str, Dict[str, object]] = {}
            for worker in merged_workers:
                for item in worker.verification_evidence:
                    key = str(item.get("check_key", ""))
                    if key:
                        latest_checks[key] = dict(item)
            final_revision = len({
                graph.get(worker.task_id).result_commit for worker in merged_workers
            })
            for item in latest_checks.values():
                # Worker-local mutation counters are not comparable after wave
                # merge. Rebind evidence from merged artifacts to the durable
                # target revision used by the evaluator.
                item["mutation_revision"] = final_revision
            evidence["verification_evidence"] = list(latest_checks.values())
            evidence["mutation_revision"] = final_revision

        # Always run a final authoritative configured command on the merged target.
        final_test = await bounded(root_test_tool.execute({}, root_tool_context))
        tests = dict(final_test.metadata)
        tests.update(ok=final_test.ok, output=final_test.content)
        capture_final()
        repository_evidence = _repository_evidence(
            workspace.path,
            repository_context,
            extra_files=[*changed_files, *plan_files],
        )
        attach_worker_evidence(repository_evidence)
        reviewer = reviewer or PRReviewAdapter(artifact_dir / "review", mode="simple")
        review = await bounded(asyncio.to_thread(
            reviewer.review,
            ReviewInput(
                specification, plan, repository, diff, tests,
                config.test_command, config.command_timeout_seconds,
            ),
        ))
        bus.emit("review_finished", decision=review.decision, issues=len(review.issues))
        evaluator = GoalEvaluator(llm, bus, tracker)
        evaluation = await bounded(evaluator.evaluate(
            specification, diff, tests, review, repository_evidence, plan,
        ))

        while not evaluation.passed and repairs < config.max_repair_iterations:
            remaining_seconds()
            repairs += 1
            feedback = evaluation.repair_feedback()
            repair_task = graph.add(
                f"Repair pass {repairs}",
                feedback or "Address evaluator feedback",
                [],
            )
            feedback_by_task[repair_task.id] = feedback
            before_workers = len(workers)
            repair_results = await execute_ready_wave(repair=True)
            new_workers = workers[before_workers:]
            if not repair_results or not any(item.patch_path for item in new_workers):
                break
            while graph.ready_tasks():
                followup_start = len(workers)
                followup_results = await execute_ready_wave(repair=True)
                followup_workers = workers[followup_start:]
                new_workers.extend(followup_workers)
                if (not followup_results
                        or not any(item.patch_path for item in followup_workers)):
                    break
            final_test = await bounded(root_test_tool.execute({}, root_tool_context))
            tests = dict(final_test.metadata)
            tests.update(ok=final_test.ok, output=final_test.content)
            capture_final()
            repository_evidence = _repository_evidence(
                workspace.path,
                repository_context,
                extra_files=[*changed_files, *plan_files],
            )
            attach_worker_evidence(repository_evidence)
            review = await bounded(asyncio.to_thread(
                reviewer.review,
                ReviewInput(
                    specification, plan, repository, diff, tests,
                    config.test_command, config.command_timeout_seconds,
                ),
            ))
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
            capture_final()
            if diff:
                bus.emit(
                    "partial_result_saved", changed_files=changed_files,
                    diff_path=str(diff_path), error=error,
                )
        except Exception as capture_exc:
            bus.emit(
                "partial_result_save_failed",
                error=f"{type(capture_exc).__name__}: {capture_exc}",
            )

    elapsed = time.monotonic() - started
    bus.emit(
        "stage3_issue_run_finished", status=status, repairs=repairs, error=error,
        duration_seconds=elapsed, changed_files=changed_files,
        waves=wave_count, max_parallelism=max_parallelism,
    )
    task_rows = [record.to_dict() for record in graph.list()] if graph else []
    report = IssueSolveReport(
        status,
        run_id,
        issue,
        specification.to_dict(),
        repository_context.to_dict(),
        plan.to_dict(),
        task_rows,
        [item.to_dict() for item in workers],
        tests,
        review.to_dict(),
        evaluation.to_dict(),
        bool(tests.get("ok", False)),
        bool(tests.get("ok", False)),
        repairs,
        sum(item.iterations for item in workers),
        tracker.tool_calls + worker_tool_calls,
        tracker.usage.input_tokens,
        tracker.usage.output_tokens,
        tracker.usage.cost,
        elapsed,
        changed_files,
        str(diff_path) if diff_path else None,
        str(event_log),
        str(workspace.path) if workspace else None,
        error,
        {
            "mode": "stage3-coordinator",
            "waves": wave_count,
            "max_concurrency": config.max_concurrency,
            "max_parallelism": max_parallelism,
            "patch_artifacts": len(patch_artifacts),
            "conflict_repairs": conflict_repairs,
            "hygiene_repairs": hygiene_repairs,
            "base_commit": base_commit,
        },
    )
    report_path, _ = report.write(artifact_dir)
    if workspace and not config.keep_worktree:
        try:
            workspace.collect_diff()
            workspace.cleanup()
        except Exception as cleanup_exc:
            bus.emit(
                "workspace_cleanup_finished", workspace=str(workspace.path),
                error=str(cleanup_exc),
            )
    return IssueSolveResult(
        status, run_id, specification, repository_context, plan, workers,
        review, evaluation, tracker.usage,
        tracker.tool_calls + worker_tool_calls, repairs, changed_files, diff, tests,
        workspace.path if workspace else None, event_log, report_path, diff_path, error,
    )
