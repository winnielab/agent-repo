import subprocess
from pathlib import Path

from repopilot.agents.reviewer import PRReviewAdapter, ReviewInput
from repopilot.planning.plan import Plan, PlanStep
from repopilot.task.specification import TaskSpecification


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_adapter_runs_existing_pr_agent_and_copies_reports(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    source = repo / "app.py"
    source.write_text("TOKEN = None\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    source.write_text('TOKEN = "sk-test-12345678901234567890"\n', encoding="utf-8")
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    review_input = ReviewInput(
        TaskSpecification("Change token", "Change token", ["change is safe"], []),
        Plan("one step", [PlanStep("edit", "Edit", "Edit app.py")]),
        repo, diff, {"ok": True, "output": "tests passed"}, "python -m unittest",
    )

    result = PRReviewAdapter(tmp_path / "review-artifacts", mode="simple").review(review_input)

    assert result.decision == "reject"
    assert any(issue.severity == "P1" for issue in result.issues)
    copied = result.raw_artifacts["paths"]
    assert copied
    assert all((tmp_path / "review-artifacts") in Path(path).parents for path in copied.values())
    assert all(Path(path).is_file() for path in copied.values())
    assert result.raw_artifacts["test_evidence_source"] == "precomputed"
