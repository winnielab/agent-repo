"""Composition root for Stage 1's isolated single-agent solve workflow."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from repopilot.agents.coder import CoderAgent
from repopilot.config import RepoPilotConfig
from repopilot.core.context import ContextManager
from repopilot.core.models import AgentResult, AgentTask
from repopilot.core.runtime import AgentRuntime, RuntimeLimits
from repopilot.execution.environment import DockerEnvironment, LocalEnvironment
from repopilot.execution.git import run_git
from repopilot.execution.permission import ApprovalCallback, PermissionManager
from repopilot.execution.workspace import Workspace, WorkspaceManager
from repopilot.llm.base import LLMClient
from repopilot.observability.events import EventBus, JSONLSubscriber
from repopilot.observability.usage import UsageTracker
from repopilot.reporting.solve_report import SolveReport
from repopilot.tools.base import ToolContext
from repopilot.tools.editing import DeleteFileTool, EditFileTool, WriteFileTool
from repopilot.tools.filesystem import GlobTool, ReadFileTool
from repopilot.tools.git import GitDiffTool, GitStatusTool
from repopilot.tools.registry import ToolRegistry
from repopilot.tools.search import SearchCodeTool
from repopilot.tools.shell import BashTool
from repopilot.tools.testing import RunTestsTool


@dataclass
class SolveResult:
    agent: AgentResult
    run_id: str
    worktree_path: Optional[Path]
    event_log: Path
    report_path: Path
    diff_path: Optional[Path]
    diff: str
    tests: dict


def _git_metadata_root(repo: Path) -> Path:
    common = Path(run_git(repo, ["rev-parse", "--git-common-dir"]).strip())
    return common if common.is_absolute() else (repo / common).resolve()


async def solve_task(
    repository: Path,
    task_description: str,
    llm: LLMClient,
    config: Optional[RepoPilotConfig] = None,
    approval_callback: Optional[ApprovalCallback] = None,
    run_id: Optional[str] = None,
) -> SolveResult:
    config = config or RepoPilotConfig()
    config.validate()
    if not task_description.strip():
        raise ValueError("task description cannot be blank")
    repository = Path(repository).resolve()
    run_id = run_id or uuid4().hex
    metadata_root = _git_metadata_root(repository) / "repopilot"
    event_log = metadata_root / "runs" / f"{run_id}.jsonl"
    report_directory = metadata_root / "reports" / run_id
    bus = EventBus(run_id, [JSONLSubscriber(event_log)])
    workspace: Optional[Workspace] = None
    started = time.monotonic()
    diff = ""
    diff_path: Optional[Path] = None
    tests = {}
    tracker = UsageTracker()
    result = AgentResult("failed", "", 0, error="workflow did not start")
    bus.emit("run_started", task=task_description, source_repo=str(repository))

    try:
        workspace = WorkspaceManager(
            config.worktree_parent,
            bus,
            hydrate_runtime_artifacts=config.hydrate_runtime_artifacts,
            max_runtime_artifact_files=config.max_runtime_artifact_files,
            max_runtime_artifact_bytes=config.max_runtime_artifact_bytes,
        ).create(repository, run_id)
        host_environment = LocalEnvironment(
            workspace.path,
            default_timeout=config.command_timeout_seconds,
            max_output_chars=config.max_command_output_chars,
        )
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
        test_tool = RunTestsTool(config.test_command, timeout=config.command_timeout_seconds,
                                 environment=host_environment)
        registry = ToolRegistry([
            GlobTool(), ReadFileTool(), SearchCodeTool(), WriteFileTool(), EditFileTool(), DeleteFileTool(),
            BashTool(), test_tool, GitStatusTool(), GitDiffTool(),
        ])
        permission = PermissionManager(
            workspace.path,
            allowed_command_prefixes=config.allowed_command_prefixes,
            approval_callback=approval_callback,
        )
        task = AgentTask(task_description, {"test_command": config.test_command})
        context = ContextManager(
            CoderAgent().system_prompt,
            task,
            report_directory,
            bus,
            char_budget=config.context_char_budget,
            large_result_chars=config.large_tool_result_chars,
        )
        tool_context = ToolContext(workspace.path, environment, run_id, bus)
        if config.baseline_aware_tests:
            await test_tool.establish_baseline(tool_context)
        runtime = AgentRuntime(
            llm, registry, permission, bus, usage=tracker,
            limits=RuntimeLimits(config.max_iterations, config.max_tool_calls,
                                 config.max_tokens, config.max_runtime_seconds,
                                 config.max_post_test_iterations,
                                 config.max_no_mutation_iterations),
        )
        result = await runtime.run(CoderAgent(), task, context, tool_context)

        # Always perform one authoritative configured test run for the report.
        final_test = await test_tool.execute({}, tool_context)
        tests = dict(final_test.metadata)
        tests.update(ok=final_test.ok, output=final_test.content)
        diff = workspace.collect_diff()
        changed_files = workspace.changed_files()
        result.changed_files = changed_files
        result.diff = diff
        if not final_test.ok and result.status == "completed":
            result.status = "failed"
            result.error = "configured tests failed after the agent stopped"
        if not diff.strip() and result.status == "completed":
            result.status = "failed"
            result.error = "agent produced no patch"
        if diff:
            report_directory.mkdir(parents=True, exist_ok=True)
            diff_path = report_directory / "changes.patch"
            diff_path.write_text(diff, encoding="utf-8")
    except Exception as exc:
        result = AgentResult("failed", result.final_text, result.iterations, tracker.usage,
                             changed_files=result.changed_files, diff=diff,
                             error=f"{type(exc).__name__}: {exc}")
    runtime_seconds = time.monotonic() - started
    bus.emit("run_finished", status=result.status, error=result.error,
             duration_seconds=runtime_seconds, changed_files=result.changed_files,
             tests_ok=tests.get("ok"))
    report = SolveReport.from_agent(
        result, task_description, run_id, workspace.path if workspace else None,
        tracker.tool_calls, runtime_seconds, tests, diff_path, event_log,
    )
    report_path, _ = report.write(report_directory)
    if workspace and not config.keep_worktree:
        try:
            if not workspace._diff_collected:
                workspace.collect_diff()
            workspace.cleanup()
        except Exception as exc:
            bus.emit("workspace_cleanup_finished", workspace=str(workspace.path), error=str(exc))
    return SolveResult(result, run_id, workspace.path if workspace else None, event_log,
                       report_path, diff_path, diff, tests)
