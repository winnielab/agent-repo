from repopilot.agents.reviewer import PRReviewAdapter, ReviewInput
from repopilot.planning.plan import Plan, PlanStep
from repopilot.task.specification import TaskSpecification


def review_input(tmp_path):
    return ReviewInput(
        TaskSpecification("Fix", "Fix it", ["works"], []),
        Plan("plan", [PlanStep("a", "Fix", "fix")]),
        tmp_path, "diff --git a/a.py b/a.py\n", {"ok": True}, "pytest",
    )


def test_adapter_normalizes_legacy_reviewer_payload(tmp_path) -> None:
    adapter = PRReviewAdapter(tmp_path / "artifacts", runner=lambda value: {
        "verdict": "request_changes", "summary": "bug remains",
        "findings": [{"title": "Wrong edge", "severity": "P1", "file": "a.py",
                      "line": 4, "evidence": "bad", "suggestion": "fix it"}],
        "paths": {"report": "/tmp/report"},
    })
    result = adapter.review(review_input(tmp_path))
    assert result.decision == "reject"
    assert result.issues[0].severity == "P1"
    assert result.suggestions == ["fix it"]


def test_adapter_approves_non_blocking_comment(tmp_path) -> None:
    adapter = PRReviewAdapter(tmp_path, runner=lambda value: {
        "verdict": "comment", "findings": [], "summary": "minor", "paths": {},
    })
    assert adapter.review(review_input(tmp_path)).decision == "approve"


def test_adapter_suppresses_baseline_and_unrequested_test_policy_noise(tmp_path) -> None:
    adapter = PRReviewAdapter(tmp_path, runner=lambda value: {
        "verdict": "request_changes", "summary": "two generic findings",
        "findings": [
            {"title": "Configured test command failed", "severity": "P1"},
            {"title": "Production code changed without nearby test changes", "severity": "P2"},
        ],
        "paths": {},
    })
    value = review_input(tmp_path)
    value.test_result.update({"ok": True, "raw_ok": False, "baseline_matched": True})
    result = adapter.review(value)
    assert result.decision == "approve"
    assert result.issues == []
    assert len(result.raw_artifacts["suppressed_findings"]) == 2


def test_adapter_keeps_missing_tests_finding_when_task_requests_tests(tmp_path) -> None:
    adapter = PRReviewAdapter(tmp_path, runner=lambda value: {
        "verdict": "request_changes",
        "findings": [{
            "title": "Production code changed without nearby test changes", "severity": "P2",
        }],
        "paths": {},
    })
    base = review_input(tmp_path)
    value = ReviewInput(
        TaskSpecification("Fix", "Add a regression test for it", ["works"], []),
        base.plan, base.workspace, base.diff, base.test_result, base.test_command,
    )
    result = adapter.review(value)
    assert result.decision == "reject"
    assert len(result.issues) == 1


def test_adapter_suppresses_test_policy_when_issue_only_requires_tests_to_pass(tmp_path) -> None:
    adapter = PRReviewAdapter(tmp_path, runner=lambda value: {
        "verdict": "comment",
        "findings": [{
            "title": "Production code changed without nearby test changes", "severity": "P2",
        }],
        "paths": {},
    })
    base = review_input(tmp_path)
    value = ReviewInput(
        TaskSpecification(
            "Fix divide so all tests pass",
            "Repair production behavior",
            ["Tests pass"],
            ["Do not modify tests to make them pass"],
        ),
        base.plan, base.workspace, base.diff, base.test_result, base.test_command,
    )

    result = adapter.review(value)

    assert result.decision == "approve"
    assert result.issues == []
