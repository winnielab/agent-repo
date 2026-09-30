import asyncio
import json
import subprocess

from repopilot.agents.reviewer import PRReviewAdapter
from repopilot.config import RepoPilotConfig
from repopilot.core.models import LLMResponse, ToolCall, Usage
from repopilot.llm.scripted import ScriptedLLM
from repopilot.workflows.solve_issue import (
    _worker_runtime_ceiling,
    _worker_token_ceiling,
    solve_issue,
)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def calculator_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "calculator.py").write_text("def divide(a, b):\n    return a * b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import divide\n"
        "class T(unittest.TestCase):\n"
        "    def test_divide(self): self.assertEqual(divide(8, 2), 4)\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return repo


def analysis_response():
    return LLMResponse(text=json.dumps({
        "title": "Fix division",
        "description": "Correct divide so it returns the quotient.",
        "acceptance_criteria": [
            "divide(8, 2) returns 4",
            "The configured unittest suite passes",
        ],
        "constraints": ["Keep the public function signature"],
        "behavior_cases": [{
            "key": "quotient",
            "scenario": "the divisor is non-zero",
            "expected": "divide returns the quotient",
            "must_preserve": "the public function signature",
        }],
    }), usage=Usage(10, 4))


def explorer_response():
    return LLMResponse(text=json.dumps({
        "language": "python", "frameworks": ["unittest"],
        "test_command": "python -m unittest discover -v",
        "important_files": ["calculator.py", "test_calculator.py"],
        "structure_summary": "A module and its unittest suite.",
        "build_system": "stdlib",
    }), usage=Usage(10, 4))


def plan_response():
    return LLMResponse(text=json.dumps({
        "summary": "Correct the implementation and verify it.",
        "root_cause_hypothesis": "the arithmetic operator violates quotient semantics",
        "earliest_invariant": "divide computes a / b before returning",
        "hypothesis_checks": [{
            "key": "operator-semantics",
            "claim": "multiplication is the first divergence from quotient semantics",
            "falsification": "trace divide(8, 2) and reject the claim if an earlier transformation changes the inputs",
            "evidence_keys": [],
        }],
        "steps": [{
            "key": "fix", "title": "Fix divide", "description": "Replace multiplication with division.",
            "dependencies": [], "behavior_keys": ["quotient"],
        }],
    }), usage=Usage(10, 4))


def evaluation_response(status="pass", evidence="diff and passing test prove the result"):
    return LLMResponse(text=json.dumps({
        "criteria": [
            {"criterion_index": 0, "status": status, "evidence": evidence},
            {"criterion_index": 1, "status": status, "evidence": "configured tests pass"},
            {
                "criterion_key": "behavior:quotient",
                "status": status,
                "evidence": evidence,
                "expected": "divide returns the quotient",
                "observed": "the candidate arithmetic result",
                "counterexample": "8 divided by 2" if status != "pass" else "",
                "invalidated_assumption": (
                    "subtraction is equivalent to division" if status != "pass" else ""
                ),
            },
            {
                "criterion_key": "hypothesis:operator-semantics",
                "status": status,
                "evidence": evidence,
                "expected": "the arithmetic operator is the first divergence",
                "observed": "divide directly returns the arithmetic expression",
                "counterexample": "the current operator is subtraction" if status != "pass" else "",
                "invalidated_assumption": (
                    "the current operator implements quotient semantics" if status != "pass" else ""
                ),
            },
        ],
        "summary": "Goal satisfied." if status == "pass" else "Division is still wrong.",
    }), usage=Usage(10, 4))


def approve_reviewer(tmp_path):
    return PRReviewAdapter(tmp_path / "review", runner=lambda value: {
        "verdict": "approve", "findings": [], "summary": "No blocking issue", "paths": {},
    })


def config(tmp_path):
    return RepoPilotConfig(
        test_command="python -m unittest discover -v",
        worktree_parent=tmp_path / "worktrees", keep_worktree=True,
        max_repair_iterations=2,
    )


def test_issue_workflow_persists_plan_memory_review_and_evaluation(tmp_path) -> None:
    repo = calculator_repo(tmp_path)
    original = (repo / "calculator.py").read_text(encoding="utf-8")
    llm = ScriptedLLM([
        analysis_response(), explorer_response(), plan_response(),
        LLMResponse(tool_calls=[ToolCall("edit", "edit_file", {
            "path": "calculator.py", "old_text": "return a * b", "new_text": "return a / b",
        })], usage=Usage(8, 2)),
        LLMResponse(text="Implemented the division fix.", usage=Usage(6, 3)),
        evaluation_response(),
    ])

    result = asyncio.run(solve_issue(
        repo, "Fix divide", llm, config(tmp_path), reviewer=approve_reviewer(tmp_path),
        run_id="stage2-success",
    ))

    assert result.success
    assert result.tests["ok"] is True
    assert result.review.decision == "approve"
    assert result.evaluation.passed
    assert result.plan.earliest_invariant == "divide computes a / b before returning"
    assert result.plan.steps[0].behavior_keys == ["quotient"]
    assert result.repair_iterations == 0
    assert result.changed_files == ["calculator.py"]
    assert "return a / b" in result.diff
    assert (repo / "calculator.py").read_text(encoding="utf-8") == original
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["tasks"][0]["status"] == "completed"
    assert report["evaluation"]["criteria"][0]["status"] == "pass"
    assert report["tests_passed"] is True
    assert report["regression_passed"] is True
    assert report["iterations"] == result.iterations == 2
    artifact_dir = result.report_path.parent
    assert list((artifact_dir / "tasks").glob("task_*.json"))
    assert (artifact_dir / "working-memory.json").is_file()
    event_types = {
        json.loads(line)["type"] for line in result.event_log.read_text(encoding="utf-8").splitlines()
    }
    assert {
        "task_analysis_finished", "repository_explored", "planning_finished",
        "review_finished", "goal_evaluation_finished", "issue_run_finished",
    } <= event_types


def test_issue_workflow_repairs_a_failed_first_attempt(tmp_path) -> None:
    repo = calculator_repo(tmp_path)
    llm = ScriptedLLM([
        analysis_response(), explorer_response(), plan_response(),
        LLMResponse(tool_calls=[ToolCall("wrong", "edit_file", {
            "path": "calculator.py", "old_text": "return a * b", "new_text": "return a - b",
        })]),
        LLMResponse(text="First attempt complete."),
        evaluation_response("fail", "implementation subtracts instead of divides"),
        LLMResponse(tool_calls=[ToolCall("repair", "edit_file", {
            "path": "calculator.py", "old_text": "return a - b", "new_text": "return a / b",
        })]),
        LLMResponse(text="Repair complete."),
        evaluation_response(),
    ])

    result = asyncio.run(solve_issue(
        repo, "Fix divide", llm, config(tmp_path), reviewer=approve_reviewer(tmp_path),
        run_id="stage2-repair",
    ))

    assert result.success
    assert result.repair_iterations == 1
    assert result.tests["ok"] is True
    assert len(result.workers) == 2
    assert all(worker.status == "completed" for worker in result.workers)
    memory = json.loads((result.report_path.parent / "working-memory.json").read_text(encoding="utf-8"))
    assert memory["test_failures"]
    assert any("Repair 1" in item for item in memory["failed_attempts"])


def test_issue_workflow_enforces_global_token_budget_between_phases(tmp_path) -> None:
    repo = calculator_repo(tmp_path)
    llm = ScriptedLLM([LLMResponse(
        text=analysis_response().text,
        usage=Usage(input_tokens=11, output_tokens=1),
    )])
    limited = RepoPilotConfig(
        test_command="python -m unittest discover -v",
        worktree_parent=tmp_path / "limited-worktrees", keep_worktree=True,
        max_tokens=10, max_repair_iterations=0,
    )

    result = asyncio.run(solve_issue(
        repo, "Fix divide", llm, limited, reviewer=approve_reviewer(tmp_path),
        run_id="stage2-token-limit",
    ))

    assert not result.success
    assert result.error == "RuntimeLimitError: issue workflow limit reached: max_tokens"
    events = [
        json.loads(line) for line in result.event_log.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["type"] == "limit_reached"
               and event["data"]["limit"] == "max_tokens" for event in events)


def test_initial_worker_reserves_tokens_for_repair_and_finalization() -> None:
    config = RepoPilotConfig(
        max_tokens=200_000,
        finalization_token_reserve=30_000,
        repair_token_reserve=40_000,
        evaluation_token_reserve=20_000,
        max_repair_iterations=2,
    )
    assert _worker_token_ceiling(config) == 110_000
    assert _worker_token_ceiling(config, repair=True) == 170_000


def test_worker_reserves_wall_time_for_final_test_command() -> None:
    config = RepoPilotConfig(max_runtime_seconds=1200, command_timeout_seconds=600)
    assert _worker_runtime_ceiling(config, 1200) == 600
    assert _worker_runtime_ceiling(config, 700) == 100


def test_issue_workflow_saves_partial_patch_when_token_limit_interrupts_finalization(tmp_path) -> None:
    repo = calculator_repo(tmp_path)
    llm = ScriptedLLM([
        analysis_response(), explorer_response(), plan_response(),
        LLMResponse(tool_calls=[ToolCall("edit", "edit_file", {
            "path": "calculator.py", "old_text": "return a * b", "new_text": "return a / b",
        })], usage=Usage(input_tokens=60, output_tokens=1)),
    ])
    limited = RepoPilotConfig(
        test_command="python -m unittest discover -v",
        worktree_parent=tmp_path / "partial-worktrees", keep_worktree=True,
        max_tokens=80, finalization_token_reserve=0, max_repair_iterations=0,
    )

    result = asyncio.run(solve_issue(
        repo, "Fix divide", llm, limited, reviewer=approve_reviewer(tmp_path),
        run_id="stage2-partial-token-limit",
    ))

    assert not result.success
    assert "max_tokens" in (result.error or "")
    assert result.changed_files == ["calculator.py"]
    assert "return a / b" in result.diff
    assert result.diff_path is not None and result.diff_path.is_file()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["changed_files"] == ["calculator.py"]
    assert report["diff_path"] == str(result.diff_path)
    events = [
        json.loads(line) for line in result.event_log.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["type"] == "partial_result_saved" for event in events)
