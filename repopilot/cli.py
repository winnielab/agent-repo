"""RepoPilot command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from repopilot.config import RepoPilotConfig
from repopilot.llm.openai_compatible import OpenAICompatibleClient, deepseek_from_environment
from repopilot.workflows.solve_issue import solve_issue
from repopilot.workflows.solve_task import solve_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repopilot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    solve = subparsers.add_parser("solve", help="solve one coding task in an isolated worktree")
    solve.add_argument("--repo", type=Path, required=True)
    solve.add_argument("--task", required=True)
    solve.add_argument("--run-id", help="stable run identifier for reproducible or resumable evaluation")
    solve.add_argument("--test-command", required=True)
    solve.add_argument("--provider", choices=("deepseek", "openai-compatible"), default="deepseek")
    solve.add_argument("--model", default="deepseek-chat")
    solve.add_argument("--base-url")
    solve.add_argument("--api-key-env", default="OPENAI_API_KEY")
    solve.add_argument("--max-iterations", type=int, default=30)
    solve.add_argument("--max-tool-calls", type=int, default=120)
    solve.add_argument("--max-tokens", type=int, default=200_000)
    solve.add_argument("--finalization-token-reserve", type=int, default=30_000,
                       help="reserve tokens for final tests, review, and goal evaluation")
    solve.add_argument("--repair-token-reserve", type=int, default=40_000,
                       help="reserve tokens for one evaluator-guided repair attempt")
    solve.add_argument("--evaluation-token-reserve", type=int, default=20_000,
                       help="reserve tokens for the first review and goal evaluation")
    solve.add_argument("--max-post-test-iterations", type=int, default=1,
                       help="maximum model follow-up turns after configured tests pass")
    solve.add_argument("--max-no-mutation-iterations", type=int, default=8,
                       help="restrict coding agents to editing tools after this many read-only turns; 0 disables")
    solve.add_argument("--max-explorer-iterations", type=int, default=12,
                       help="request read-only repository exploration finalization after this many turns")
    solve.add_argument("--max-explorer-tool-calls", type=int, default=40)
    solve.add_argument("--max-explorer-tokens", type=int, default=120_000)
    solve.add_argument("--context-char-budget", type=int, default=80_000)
    solve.add_argument("--large-tool-result-chars", type=int, default=12_000)
    solve.add_argument("--baseline-aware-tests", action="store_true",
                       help="accept unchanged recognized test failures that already exist before editing")
    solve.add_argument(
        "--no-hydrate-runtime-artifacts",
        dest="hydrate_runtime_artifacts",
        action="store_false",
        default=True,
        help="do not copy ignored native extension modules from the prepared repository into the worktree",
    )
    solve.add_argument("--max-runtime-artifact-files", type=int, default=4096)
    solve.add_argument("--max-runtime-artifact-bytes", type=int, default=1_073_741_824)
    solve.add_argument("--execution-image",
                       help="run agent shell commands inside this Docker image")
    solve.add_argument("--execution-platform",
                       help="optional Docker platform, for example linux/amd64")
    solve.add_argument("--execution-setup-command", default="",
                       help="shell prefix run before each command inside the execution image")
    solve.add_argument("--max-runtime-seconds", type=float, default=120.0)
    solve.add_argument("--command-timeout-seconds", type=float, default=30.0)
    solve.add_argument("--worktree-parent", type=Path)
    solve.add_argument("--workflow", choices=("issue", "task"), default="issue",
                       help="issue runs Stage 2 planning/review/evaluation; task runs the Stage 1 coder")
    solve.add_argument("--max-repair-iterations", type=int, default=3)
    solve.add_argument("--max-cost", type=float)
    solve.add_argument("--cleanup", action="store_true", help="remove the worktree after collecting its patch")
    return parser


async def _solve(args: argparse.Namespace) -> int:
    if args.provider == "deepseek":
        client = deepseek_from_environment(args.model)
    else:
        key = os.environ.get(args.api_key_env, "")
        if not key or not args.base_url:
            raise ValueError("openai-compatible requires --base-url and the configured API key environment variable")
        client = OpenAICompatibleClient(key, args.model, args.base_url)
    config = RepoPilotConfig(
        max_iterations=args.max_iterations,
        max_tool_calls=args.max_tool_calls,
        max_tokens=args.max_tokens,
        finalization_token_reserve=args.finalization_token_reserve,
        repair_token_reserve=args.repair_token_reserve,
        evaluation_token_reserve=args.evaluation_token_reserve,
        max_post_test_iterations=args.max_post_test_iterations,
        max_no_mutation_iterations=args.max_no_mutation_iterations,
        max_explorer_iterations=args.max_explorer_iterations,
        max_explorer_tool_calls=args.max_explorer_tool_calls,
        max_explorer_tokens=args.max_explorer_tokens,
        execution_image=args.execution_image,
        execution_platform=args.execution_platform,
        execution_setup_command=args.execution_setup_command,
        max_runtime_seconds=args.max_runtime_seconds,
        command_timeout_seconds=args.command_timeout_seconds,
        context_char_budget=args.context_char_budget,
        large_tool_result_chars=args.large_tool_result_chars,
        baseline_aware_tests=args.baseline_aware_tests,
        hydrate_runtime_artifacts=args.hydrate_runtime_artifacts,
        max_runtime_artifact_files=args.max_runtime_artifact_files,
        max_runtime_artifact_bytes=args.max_runtime_artifact_bytes,
        test_command=args.test_command,
        worktree_parent=args.worktree_parent,
        keep_worktree=not args.cleanup,
        max_repair_iterations=args.max_repair_iterations,
        max_cost=args.max_cost,
    )
    if args.workflow == "task":
        result = await solve_task(args.repo, args.task, client, config=config, run_id=args.run_id)
        status = result.agent.status
        success = result.agent.success
        changed_files = result.agent.changed_files
        error = result.agent.error
    else:
        result = await solve_issue(args.repo, args.task, client, config=config, run_id=args.run_id)
        status = result.status
        success = result.success
        changed_files = result.changed_files
        error = result.error
    print(json.dumps({
        "status": status,
        "run_id": result.run_id,
        "report": str(result.report_path),
        "worktree": str(result.worktree_path) if result.worktree_path else None,
        "changed_files": changed_files,
        "error": error,
    }, ensure_ascii=False, indent=2))
    return 0 if success else 1


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_solve(args))
    except (OSError, ValueError) as exc:
        print(f"repopilot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
