"""Deterministic Stage 1 benchmark adapter for harness E2E tests only.

This is a protocol fixture, not an autonomous-model score. It knows the ten
public fixture edits and exists to prove that RepoPilot's loop, worktrees,
events, patch serialization, and benchmark bridge compose correctly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from repopilot.config import RepoPilotConfig
from repopilot.core.models import LLMResponse, ToolCall, Usage
from repopilot.llm.scripted import ScriptedLLM
from repopilot.workflows.solve_task import solve_task


FIXTURES = {
    "Fix divide() so all tests pass.": ("calculator.py", "return a * b", "return a / b"),
    "Pagination returns one item fewer than requested. Fix it without breaking boundaries.": (
        "pagination.py", "return items[offset:offset + limit - 1]", "return items[offset:offset + limit]",
    ),
    "average() crashes for empty input; make it follow the documented empty-input behavior.": (
        "statistics.py", "return sum(values) / len(values)",
        "return sum(values) / len(values) if values else 0.0",
    ),
    "User lookup should be case-insensitive while returning the stored user record unchanged.": (
        "users.py", 'if user["username"] == username:',
        'if user["username"].casefold() == username.casefold():',
    ),
    "Fix clamp() at and outside both boundaries.": (
        "numbers.py", "if value < minimum:\n        return maximum",
        "if value < minimum:\n        return minimum",
    ),
    "deduplicate() must remove duplicates while preserving first-seen order.": (
        "collections_utils.py", "return list(set(items))", "return list(dict.fromkeys(items))",
    ),
    "CSV records should expose trimmed header names without stripping field values.": (
        "csv_reader.py", "reader = csv.DictReader(io.StringIO(text))\n    return list(reader)",
        "reader = csv.DictReader(io.StringIO(text))\n    if reader.fieldnames is not None:\n        reader.fieldnames = [name.strip() for name in reader.fieldnames]\n    return list(reader)",
    ),
    "Boolean configuration strings are parsed incorrectly. Implement the documented true/false behavior.": (
        "config.py", "return bool(value)",
        "normalized = value.strip().casefold()\n    if normalized == \"true\":\n        return True\n    if normalized == \"false\":\n        return False\n    raise ValueError(f\"invalid boolean value: {value!r}\")",
    ),
    "Rendered order summaries show customer and order identifiers in the wrong places.": (
        "service.py", 'return format_order(order["customer_id"], order["order_id"])',
        'return format_order(order["order_id"], order["customer_id"])',
    ),
    "parse_port() should fall back for invalid numeric strings without hiding unrelated errors.": (
        "parser.py", "except TypeError:", "except ValueError:",
    ),
}


def scripted_client(problem):
    path, old_text, new_text = FIXTURES[problem]
    return ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("read", "read_file", {"path": path})], usage=Usage(10, 2)),
        LLMResponse(tool_calls=[ToolCall("edit", "edit_file", {
            "path": path, "old_text": old_text, "new_text": new_text,
        })], usage=Usage(12, 3)),
        LLMResponse(tool_calls=[ToolCall("tests", "run_tests", {}),
                                ToolCall("diff", "git_diff", {})], usage=Usage(15, 4)),
        LLMResponse(text="Implemented the focused fix and verified the configured tests.",
                    usage=Usage(8, 4), stop_reason="stop"),
    ])


async def solve(request):
    result = await solve_task(
        Path(request["repository"]),
        request["problem_statement"],
        scripted_client(request["problem_statement"]),
        config=RepoPilotConfig(
            max_iterations=request["max_iterations"],
            command_timeout_seconds=5,
            test_command=request["test_command"],
            worktree_parent=Path(request["worktree_parent"]),
            keep_worktree=True,
        ),
        approval_callback=lambda call, reason: False,
    )
    return {
        "agent_completed": result.agent.success,
        "patch": result.diff,
        "event_log": str(result.event_log),
        "worktree_path": str(result.worktree_path),
        "iterations": result.agent.iterations,
        "input_tokens": result.agent.usage.input_tokens,
        "output_tokens": result.agent.usage.output_tokens,
        "error": result.agent.error,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        response = asyncio.run(solve(request))
    except Exception as exc:
        response = {"agent_completed": False, "error": f"{type(exc).__name__}: {exc}"}
    Path(args.response).write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if response["agent_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
