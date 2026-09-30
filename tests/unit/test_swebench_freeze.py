from pathlib import Path

import pytest

from scripts.freeze_swebench_subset import freeze, public_test_command
from scripts.run_swebench_serial import (
    docker_test_command,
    inspect_prepared_image,
    swebench_eval_command,
)


def _task(root: Path, instance_id: str, repo: str) -> None:
    directory = root / "tasks" / instance_id
    directory.mkdir(parents=True)
    (directory / "task.yaml").write_text(
        "\n".join([
            "base_commit: abc123",
            "difficulty: 15 min - 1 hour",
            "image: example/image:latest",
            f"instance_id: {instance_id}",
            f"repo: {repo}",
            "split: test",
            "version: '1.0'",
            "datasets:",
            "- SWE-bench/SWE-bench_Verified",
        ]) + "\n",
        encoding="utf-8",
    )
    (directory / "eval.sh").write_text(
        ": '>>>>> Start Test Output'\npytest -q tests/test_x.py\n"
        ": '>>>>> End Test Output'\n",
        encoding="utf-8",
    )


def test_public_test_command_extracts_only_marked_section(tmp_path):
    path = tmp_path / "eval.sh"
    path.write_text(
        "setup\n: '>>>>> Start Test Output'\npytest -q\n: '>>>>> End Test Output'\nteardown\n",
        encoding="utf-8",
    )
    assert public_test_command(path) == "pytest -q"


def test_freeze_is_deterministic_diverse_and_excludes_instance(tmp_path):
    _task(tmp_path, "alpha__repo-1", "alpha/repo")
    _task(tmp_path, "alpha__repo-2", "alpha/repo")
    _task(tmp_path, "beta__repo-1", "beta/repo")
    _task(tmp_path, "beta__repo-2", "beta/repo")
    first = freeze(tmp_path, 2, "fixed-seed", {"alpha__repo-1"})
    second = freeze(tmp_path, 2, "fixed-seed", {"alpha__repo-1"})
    ids = [item["instance_id"] for item in first["instances"]]
    assert first["instance_ids_sha256"] == second["instance_ids_sha256"]
    assert ids[0] == "alpha__repo-2"
    assert ids[1].startswith("beta__repo-")
    assert first["selection_policy"]["replacement_after_freeze"] is False


def test_docker_test_command_mounts_active_worktree_at_runtime():
    command = docker_test_command("example/image:latest", "pytest -q tests/test_x.py")
    assert 'src=$PWD,dst=/testbed' in command
    assert "example/image:latest" in command
    assert "pytest -q tests/test_x.py" in command


def test_swebench_eval_command_can_trust_prepared_image_without_rebuild():
    command = swebench_eval_command(
        Path("/opt/swebench"),
        instance_id="pytest-dev__pytest-7324",
        run_id="gold-19",
        task_repo=None,
    )

    assert command == [
        "/opt/swebench", "eval", "verified", "--gold", "-i",
        "pytest-dev__pytest-7324", "--run-id", "gold-19", "-j", "1",
    ]
    assert "--task-repo" not in command


def test_swebench_eval_command_keeps_task_repo_for_local_build_mode():
    command = swebench_eval_command(
        Path("/opt/swebench"),
        instance_id="pytest-dev__pytest-7324",
        run_id="eval-19",
        task_repo=Path("/tasks"),
        predictions_path=Path("/tmp/prediction.jsonl"),
    )

    assert command == [
        "/opt/swebench", "eval", "verified", "-p", "/tmp/prediction.jsonl",
        "-i", "pytest-dev__pytest-7324", "--run-id", "eval-19",
        "--task-repo", "/tasks", "-j", "1",
    ]


def test_inspect_prepared_image_accepts_exact_amd64_metadata(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_swebench_serial.capture",
        lambda command: (
            '{"Architecture":"amd64","Id":"sha256:abc","Size":42,'
            '"RepoDigests":["example/image@sha256:def"]}'
        ),
    )

    result = inspect_prepared_image("example/image:latest")

    assert result == {
        "image": "example/image:latest",
        "id": "sha256:abc",
        "architecture": "amd64",
        "size": 42,
        "repo_digests": ["example/image@sha256:def"],
    }


def test_inspect_prepared_image_rejects_host_architecture(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_swebench_serial.capture",
        lambda command: '{"Architecture":"arm64","Id":"sha256:abc"}',
    )

    with pytest.raises(RuntimeError, match="expected 'amd64'"):
        inspect_prepared_image("example/image:latest")
