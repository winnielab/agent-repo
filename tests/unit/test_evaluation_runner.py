import asyncio
import json

from repopilot.evaluation import BenchmarkTask, EvaluationRecord, EvaluationRunner


def test_evaluation_runner_writes_measured_artifacts_and_resumes(tmp_path) -> None:
    calls = []

    async def evaluate(task, variant):
        calls.append(task.id)
        return EvaluationRecord(
            task.id, True, patch_applied=True, tests_passed=True,
            regression_passed=True, tool_calls=3, input_tokens=10,
            output_tokens=2, changed_files=["src/x.py"], variant=variant,
        )

    task = BenchmarkTask(
        "case-1", "/repo", "abc", "fix it", "pytest -q",
        expected_files=["src/x.py"],
    )
    runner = EvaluationRunner(tmp_path / "reports", evaluate)
    records = asyncio.run(runner.run([task], variant="full", resume=True))
    resumed = asyncio.run(runner.run([task], variant="full", resume=True))

    assert calls == ["case-1"]
    assert records[0].localization_accuracy == 1.0
    assert resumed[0].success
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text())
    assert summary["resolve_rate"] == 1.0
    assert summary["non_empty_patch_rate"] == 1.0
    assert summary["candidate_test_pass_rate"] == 1.0
    assert summary["average_tool_calls"] == 3.0
    for name in ("tasks.jsonl", "metrics.csv", "report.md"):
        assert (tmp_path / "reports" / name).is_file()
