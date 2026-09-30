import json

import pytest

from scripts.swebench_prediction import build_prediction


def test_build_prediction_reads_resolved_patch(tmp_path) -> None:
    patch = tmp_path / "changes.patch"
    patch.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    report = tmp_path / "issue-report.json"
    report.write_text(json.dumps({"status": "resolved", "diff_path": str(patch)}), encoding="utf-8")
    prediction = build_prediction(report, "owner__repo-1", "repopilot")
    assert prediction == {
        "instance_id": "owner__repo-1",
        "model_name_or_path": "repopilot",
        "model_patch": "diff --git a/a.py b/a.py\n",
    }


def test_build_prediction_rejects_failed_or_empty_reports(tmp_path) -> None:
    report = tmp_path / "issue-report.json"
    report.write_text(json.dumps({"status": "failed", "diff_path": None}), encoding="utf-8")
    with pytest.raises(ValueError, match="not resolved"):
        build_prediction(report, "owner__repo-1", "repopilot")


def test_build_prediction_can_emit_partial_patch_for_external_scoring(tmp_path) -> None:
    patch = tmp_path / "changes.patch"
    patch.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    report = tmp_path / "issue-report.json"
    report.write_text(json.dumps({"status": "failed", "diff_path": str(patch)}), encoding="utf-8")
    prediction = build_prediction(
        report, "owner__repo-1", "repopilot", require_resolved=False,
    )
    assert prediction["model_patch"] == "diff --git a/a.py b/a.py\n"
