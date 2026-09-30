"""Structured adapter for repopilot-test-benchmark's request/response protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def solve(request):
    product = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(product))
    from repopilot.config import RepoPilotConfig
    from repopilot.llm.openai_compatible import deepseek_from_environment
    from repopilot.workflows.solve_task import solve_task

    if request["provider"] != "deepseek":
        raise ValueError("benchmark adapter currently supports provider=deepseek")
    client = deepseek_from_environment(request["model"])
    config = RepoPilotConfig(
        max_iterations=request["max_iterations"],
        command_timeout_seconds=5,
        test_command=request["test_command"],
        worktree_parent=Path(request["worktree_parent"]),
        keep_worktree=True,
        allowed_command_prefixes=(
            "ls", "pwd", "find", "rg", "grep", "cat", "head", "tail",
            "sed", "wc", "file", "python", "python3", "git",
        ),
    )
    result = await solve_task(
        Path(request["repository"]), request["problem_statement"], client,
        config=config, approval_callback=lambda call, reason: False,
    )
    return {
        "agent_completed": result.agent.success,
        "patch": result.diff,
        "event_log": str(result.event_log),
        "worktree_path": str(result.worktree_path) if result.worktree_path else None,
        "iterations": result.agent.iterations,
        "input_tokens": result.agent.usage.input_tokens,
        "output_tokens": result.agent.usage.output_tokens,
        "final_text": result.agent.final_text,
        "error": result.agent.error,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = asyncio.run(solve(request))
    except Exception as exc:
        result = {"agent_completed": False, "error": f"{type(exc).__name__}: {exc}"}
    Path(args.response).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result["agent_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
