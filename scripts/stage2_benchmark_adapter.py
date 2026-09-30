"""Benchmark protocol adapter for the Stage 2 autonomous issue workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from repopilot.config import RepoPilotConfig
from repopilot.llm.openai_compatible import deepseek_from_environment
from repopilot.workflows.solve_issue import solve_issue


async def solve(request):
    if request["provider"] != "deepseek":
        raise ValueError("Stage 2 benchmark adapter currently supports provider=deepseek")
    result = await solve_issue(
        Path(request["repository"]),
        request["problem_statement"],
        deepseek_from_environment(request["model"]),
        config=RepoPilotConfig(
            max_iterations=request["max_iterations"],
            max_repair_iterations=2,
            command_timeout_seconds=5,
            test_command=request["test_command"],
            worktree_parent=Path(request["worktree_parent"]),
            keep_worktree=True,
            allowed_command_prefixes=(
                "ls", "pwd", "find", "rg", "grep", "cat", "head", "tail",
                "sed", "wc", "file", "python", "python3", "git",
            ),
        ),
        approval_callback=lambda call, reason: False,
    )
    patch = result.diff.rstrip("\n") + "\n" if result.diff.strip() else ""
    return {
        "agent_completed": result.success,
        "patch": patch,
        "event_log": str(result.event_log),
        "worktree_path": str(result.worktree_path) if result.worktree_path else None,
        "iterations": result.iterations,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "repair_iterations": result.repair_iterations,
        "reviewer_decision": result.review.decision,
        "acceptance_criteria": [item.to_dict() for item in result.evaluation.criteria],
        "report": str(result.report_path),
        "error": result.error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        response = asyncio.run(solve(request))
    except Exception as exc:
        response = {"agent_completed": False, "error": f"{type(exc).__name__}: {exc}"}
    Path(args.response).write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return 0 if response["agent_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
