"""Run a frozen SWE-bench subset one task at a time with resumable artifacts."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.swebench_prediction import build_prediction
except ModuleNotFoundError:  # Direct execution with scripts/ as sys.path[0].
    from swebench_prediction import build_prediction


SETUP_COMMAND = (
    "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def docker_test_command(image: str, public_test_command: str) -> str:
    """Build a host test command that mounts RepoPilot's active worktree."""
    inner = f"{SETUP_COMMAND} && {public_test_command}"
    return (
        "docker run --rm --platform linux/amd64 "
        '--mount "type=bind,src=$PWD,dst=/testbed" '
        f"-w /testbed {shlex.quote(image)} bash -lc {shlex.quote(inner)}"
    )


def swebench_eval_command(
    swebench: Path,
    *,
    instance_id: str,
    run_id: str,
    task_repo: Path | None,
    predictions_path: Path | None = None,
) -> list[str]:
    """Build an eval command, optionally trusting a preflighted local image.

    Passing ``--task-repo`` makes current SWE-bench versions force a Buildx
    rebuild. On Apple Silicon, callers that already pulled and verified the
    exact linux/amd64 image must omit it so the preflighted image is preserved.
    """
    command = [str(swebench), "eval", "verified"]
    if predictions_path is None:
        command.append("--gold")
    else:
        command.extend(["-p", str(predictions_path)])
    command.extend(["-i", instance_id, "--run-id", run_id])
    if task_repo is not None:
        command.extend(["--task-repo", str(task_repo)])
    command.extend(["-j", "1"])
    return command


def load_env(path: Path, environment: dict[str, str]) -> dict[str, str]:
    result = dict(environment)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def run_logged(command: list[str], log_path: Path, *, cwd: Path | None = None,
               env: dict[str, str] | None = None, check: bool = False) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"$ {shlex.join(command)}", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
        return_code = process.wait()
    if check and return_code:
        raise subprocess.CalledProcessError(return_code, command)
    return return_code


def capture(command: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.run(
        command, cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def inspect_prepared_image(image: str) -> dict[str, object]:
    """Fail closed unless the exact prepared image exists as linux/amd64."""
    payload = json.loads(capture([
        "docker", "image", "inspect", image, "--format", "{{json .}}",
    ]))
    architecture = payload.get("Architecture")
    if architecture != "amd64":
        raise RuntimeError(
            f"prepared image has architecture {architecture!r}, expected 'amd64': {image}"
        )
    return {
        "image": image,
        "id": payload.get("Id"),
        "architecture": architecture,
        "size": payload.get("Size"),
        "repo_digests": payload.get("RepoDigests") or [],
    }


def official_result(swebench_root: Path, run_id: str, instance_id: str) -> dict:
    root = swebench_root / "logs" / "evaluation" / run_id
    results_path = root / "results.json"
    if not results_path.is_file():
        return {"available": False, "results_path": str(results_path)}
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    resolved = instance_id in payload.get("resolved_ids", [])
    return {
        "available": True,
        "resolved": resolved,
        "results_path": str(results_path),
        "infrastructure_failure": instance_id in payload.get("infra_failure_ids", []),
        "error": instance_id in payload.get("error_ids", []),
    }


def ensure_workspace(workspace: Path, image: str, base_commit: str, log_dir: Path) -> None:
    if (workspace / ".git").is_dir():
        head = capture(["git", "rev-parse", "HEAD"], cwd=workspace)
        if head != base_commit:
            raise RuntimeError(
                f"existing workspace is at {head}, expected {base_commit}: {workspace}"
            )
        if capture(["git", "status", "--porcelain"], cwd=workspace):
            raise RuntimeError(f"existing workspace is dirty: {workspace}")
        return
    if workspace.exists() and any(workspace.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty workspace: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    container_id = capture([
        "docker", "create", "--platform", "linux/amd64", image, "true"
    ])
    try:
        run_logged(
            ["docker", "cp", f"{container_id}:/testbed/.", str(workspace)],
            log_dir / "workspace-copy.log",
            check=True,
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    run_logged(
        ["git", "checkout", "--detach", base_commit],
        log_dir / "workspace-checkout.log",
        cwd=workspace,
        check=True,
    )
    dirty = capture(["git", "status", "--porcelain"], cwd=workspace)
    if dirty:
        raise RuntimeError(f"new workspace is dirty after checkout: {workspace}\n{dirty}")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_one(task: dict, args: argparse.Namespace, manifest: dict,
            child_env: dict[str, str]) -> dict:
    ordinal = int(task["ordinal"])
    instance_id = task["instance_id"]
    slug = f"{ordinal:02d}-{instance_id}"
    result_dir = args.output_root / slug
    result_path = result_dir / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        print(f"[{ordinal:02d}] resume: {instance_id} already recorded", flush=True)
        return result

    result_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    started = time.monotonic()
    gold_run_id = f"{args.run_prefix}-gold-{ordinal:02d}"
    eval_run_id = f"{args.run_prefix}-eval-{ordinal:02d}"
    agent_run_id = f"{args.run_prefix}-agent-{ordinal:02d}-{instance_id}"
    result: dict = {
        "ordinal": ordinal,
        "instance_id": instance_id,
        "manifest_sha256": manifest["instance_ids_sha256"],
        "started_at": started_at,
        "gold_run_id": gold_run_id,
        "agent_run_id": agent_run_id,
        "evaluation_run_id": eval_run_id,
        "status": "running",
    }
    write_json(result_dir / "state.json", result)
    if args.use_prepared_images:
        try:
            result["prepared_image"] = inspect_prepared_image(task["image"])
        except (subprocess.CalledProcessError, json.JSONDecodeError, RuntimeError) as exc:
            result.update({
                "status": "infrastructure_failure",
                "finished_at": utc_now(),
                "runtime_seconds": time.monotonic() - started,
                "reason": f"prepared linux/amd64 image is unavailable: {exc}",
            })
            write_json(result_path, result)
            return result
    print(f"\n[{ordinal:02d}] {instance_id}: gold environment preflight", flush=True)
    gold_command = swebench_eval_command(
        args.swebench,
        instance_id=instance_id,
        run_id=gold_run_id,
        task_repo=None if args.use_prepared_images else args.task_repo,
    )
    gold_rc = run_logged(gold_command, result_dir / "gold.log", cwd=args.swebench_root)
    gold = official_result(args.swebench_root, gold_run_id, instance_id)
    result["gold"] = {"return_code": gold_rc, **gold}
    if gold_rc != 0 or not gold.get("resolved"):
        status = "infrastructure_failure" if gold.get("infrastructure_failure") else "gold_preflight_failed"
        result.update({
            "status": status,
            "finished_at": utc_now(),
            "runtime_seconds": time.monotonic() - started,
            "reason": "gold patch did not resolve in the current environment; frozen task is not replaced",
        })
        write_json(result_path, result)
        return result

    workspace = args.workspace_root / instance_id
    print(f"[{ordinal:02d}] {instance_id}: materialize clean base workspace", flush=True)
    ensure_workspace(workspace, task["image"], task["base_commit"], result_dir)
    problem_path = args.task_repo / "tasks" / instance_id / "problem_statement.md"
    problem = problem_path.read_text(encoding="utf-8")
    docker_test = docker_test_command(task["image"], task["public_test_command"])
    report_path = workspace / ".git" / "repopilot" / "issues" / agent_run_id / "issue-report.json"
    repopilot_bin = args.repopilot_root / ".venv" / "bin" / "repopilot"
    solve_command = [
        str(repopilot_bin), "solve", "--repo", str(workspace), "--task", problem,
        "--run-id", agent_run_id, "--test-command", docker_test,
        "--provider", "deepseek", "--model", args.model,
        "--max-iterations", str(args.max_iterations),
        "--max-tool-calls", str(args.max_tool_calls),
        "--max-tokens", str(args.max_tokens),
        "--finalization-token-reserve", str(args.finalization_token_reserve),
        "--repair-token-reserve", str(args.repair_token_reserve),
        "--evaluation-token-reserve", str(args.evaluation_token_reserve),
        "--max-post-test-iterations", str(args.max_post_test_iterations),
        "--max-no-mutation-iterations", str(args.max_no_mutation_iterations),
        "--max-explorer-iterations", str(args.max_explorer_iterations),
        "--max-explorer-tool-calls", str(args.max_explorer_tool_calls),
        "--max-explorer-tokens", str(args.max_explorer_tokens),
        "--context-char-budget", str(args.context_char_budget),
        "--large-tool-result-chars", str(args.large_tool_result_chars),
        "--execution-image", task["image"], "--execution-platform", "linux/amd64",
        "--execution-setup-command", SETUP_COMMAND,
        "--max-runtime-seconds", str(args.max_runtime_seconds),
        "--command-timeout-seconds", str(args.command_timeout_seconds),
        "--max-repair-iterations", str(args.max_repair_iterations), "--cleanup",
    ]
    if args.baseline_aware_tests:
        solve_command.append("--baseline-aware-tests")
    print(f"[{ordinal:02d}] {instance_id}: RepoPilot solve", flush=True)
    solve_rc = run_logged(
        solve_command, result_dir / "solve.log", cwd=args.repopilot_root, env=child_env
    )
    result["repopilot"] = {
        "return_code": solve_rc,
        "report_path": str(report_path),
    }
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        result["repopilot"].update({
            "status": report.get("status"),
            "error": report.get("error"),
            "input_tokens": report.get("input_tokens"),
            "output_tokens": report.get("output_tokens"),
            "cost": report.get("cost"),
            "tool_calls": report.get("tool_calls"),
            "runtime_seconds": report.get("runtime_seconds"),
            "changed_files": report.get("changed_files"),
            "diff_path": report.get("diff_path"),
        })

    prediction_path = result_dir / "prediction.jsonl"
    try:
        prediction = build_prediction(
            report_path, instance_id, args.model_name, require_resolved=False
        )
        prediction_path.write_text(
            json.dumps(prediction, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        result.update({
            "status": "empty_patch",
            "finished_at": utc_now(),
            "runtime_seconds": time.monotonic() - started,
            "reason": str(exc),
        })
        write_json(result_path, result)
        return result

    print(f"[{ordinal:02d}] {instance_id}: official SWE-bench evaluation", flush=True)
    eval_command = swebench_eval_command(
        args.swebench,
        instance_id=instance_id,
        run_id=eval_run_id,
        task_repo=None if args.use_prepared_images else args.task_repo,
        predictions_path=prediction_path,
    )
    eval_rc = run_logged(eval_command, result_dir / "evaluation.log", cwd=args.swebench_root)
    evaluation = official_result(args.swebench_root, eval_run_id, instance_id)
    result["evaluation"] = {"return_code": eval_rc, **evaluation}
    result.update({
        "status": "resolved" if evaluation.get("resolved") else "unresolved",
        "finished_at": utc_now(),
        "runtime_seconds": time.monotonic() - started,
        "prediction_path": str(prediction_path),
    })
    write_json(result_path, result)
    return result


def write_summary(manifest: dict, results: list[dict], output_root: Path) -> None:
    statuses: dict[str, int] = {}
    for result in results:
        status = result.get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    agent_attempts = [item for item in results if item.get("gold", {}).get("resolved")]
    evaluated = [item for item in results if item.get("evaluation", {}).get("available")]
    input_tokens = sum(item.get("repopilot", {}).get("input_tokens") or 0 for item in agent_attempts)
    output_tokens = sum(item.get("repopilot", {}).get("output_tokens") or 0 for item in agent_attempts)
    summary = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "manifest_name": manifest["name"],
        "manifest_sha256": manifest["instance_ids_sha256"],
        "selected_count": manifest["count"],
        "executed_count": len(results),
        "status_counts": statuses,
        "resolved_count": statuses.get("resolved", 0),
        "gold_valid_count": len(agent_attempts),
        "gold_preflight_failed_count": statuses.get("gold_preflight_failed", 0),
        "agent_attempted_count": len(agent_attempts),
        "officially_evaluated_patch_count": len(evaluated),
        "empty_patch_count": statuses.get("empty_patch", 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "resolve_rate_all_frozen_executed": (
            statuses.get("resolved", 0) / len(results) if results else 0.0
        ),
        "resolve_rate_gold_valid_agent_attempts": (
            statuses.get("resolved", 0) / len(agent_attempts) if agent_attempts else 0.0
        ),
        "twenty_task_gate_complete": len(results) == manifest["count"],
        "results": [
            {
                "ordinal": item["ordinal"],
                "instance_id": item["instance_id"],
                "status": item.get("status"),
                "runtime_seconds": item.get("runtime_seconds"),
            }
            for item in results
        ],
    }
    write_json(output_root / "summary.json", summary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--task-repo", type=Path, required=True)
    parser.add_argument("--swebench-root", type=Path, required=True)
    parser.add_argument("--repopilot-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--run-prefix", default="repopilot-stage2-medium-v1")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--model-name", default="repopilot-deepseek-stage2-medium-v1")
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--max-tool-calls", type=int, default=120)
    parser.add_argument("--max-tokens", type=int, default=200_000)
    parser.add_argument("--finalization-token-reserve", type=int, default=30_000)
    parser.add_argument("--repair-token-reserve", type=int, default=40_000)
    parser.add_argument("--evaluation-token-reserve", type=int, default=20_000)
    parser.add_argument("--max-post-test-iterations", type=int, default=1)
    parser.add_argument("--max-no-mutation-iterations", type=int, default=8)
    parser.add_argument("--max-explorer-iterations", type=int, default=12)
    parser.add_argument("--max-explorer-tool-calls", type=int, default=40)
    parser.add_argument("--max-explorer-tokens", type=int, default=120_000)
    parser.add_argument("--context-char-budget", type=int, default=60_000)
    parser.add_argument("--large-tool-result-chars", type=int, default=8_000)
    parser.add_argument("--baseline-aware-tests", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument(
        "--use-prepared-images",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Require an existing linux/amd64 image and omit --task-repo from "
            "SWE-bench eval so Buildx cannot replace the preflighted image"
        ),
    )
    parser.add_argument("--max-runtime-seconds", type=int, default=1200)
    parser.add_argument("--command-timeout-seconds", type=int, default=300)
    parser.add_argument("--max-repair-iterations", type=int, default=2)
    args = parser.parse_args()
    for name in (
        "manifest", "task_repo", "swebench_root", "repopilot_root", "workspace_root",
        "output_root", "env_file",
    ):
        setattr(args, name, getattr(args, name).resolve())
    args.swebench = args.swebench_root / ".venv" / "bin" / "swebench"
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.start < 1 or args.count < 1:
        raise SystemExit("--start and --count must be positive")
    selected = manifest["instances"][args.start - 1:args.start - 1 + args.count]
    if len(selected) != args.count:
        raise SystemExit("requested range exceeds frozen manifest")
    child_env = load_env(args.env_file, os.environ)
    if not child_env.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is missing from env file")
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.workspace_root.mkdir(parents=True, exist_ok=True)
    results = []
    for task in selected:
        result = run_one(task, args, manifest, child_env)
        results.append(result)
        print(
            f"[{task['ordinal']:02d}] {task['instance_id']}: {result['status']}",
            flush=True,
        )
    all_results = []
    for task in manifest["instances"]:
        result_path = args.output_root / f"{task['ordinal']:02d}-{task['instance_id']}" / "result.json"
        if result_path.is_file():
            all_results.append(json.loads(result_path.read_text(encoding="utf-8")))
    write_summary(manifest, all_results, args.output_root)
    return 0 if all(item["status"] != "infrastructure_failure" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
