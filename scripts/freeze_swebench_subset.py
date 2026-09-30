"""Freeze a deterministic, repository-diverse SWE-bench Verified medium subset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


FIELDS = ("base_commit", "image", "instance_id", "repo", "split", "version")


def parse_task(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    data = {}
    for key in FIELDS:
        match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", text)
        if not match:
            raise ValueError(f"missing {key}: {path}")
        data[key] = match.group(1).strip().strip("'").strip('"')
    difficulty = re.search(r"(?m)^difficulty:\s*(.+?)\s*$", text)
    data["difficulty"] = (
        difficulty.group(1).strip().strip("'").strip('"') if difficulty else ""
    )
    data["verified"] = "- SWE-bench/SWE-bench_Verified" in text
    return data


def public_test_command(eval_path: Path) -> str:
    lines = eval_path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if "Start Test Output" in line)
    end = next(index for index, line in enumerate(lines[start + 1:], start + 1)
               if "End Test Output" in line)
    command = "\n".join(line for line in lines[start + 1:end] if line.strip()).strip()
    if not command:
        raise ValueError(f"no public test command found: {eval_path}")
    return command


def freeze(task_repo: Path, count: int, seed: str, excluded: set[str]) -> dict:
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for yaml_path in sorted((task_repo / "tasks").glob("*/task.yaml")):
        task = parse_task(yaml_path)
        if not task["verified"] or task["difficulty"] != "15 min - 1 hour":
            continue
        if task["instance_id"] in excluded:
            continue
        task["public_test_command"] = public_test_command(yaml_path.with_name("eval.sh"))
        task.pop("verified")
        by_repo[task["repo"]].append(task)
    for tasks in by_repo.values():
        tasks.sort(key=lambda item: hashlib.sha256(
            f"{seed}:{item['instance_id']}".encode("utf-8")
        ).hexdigest())
    chosen = []
    round_index = 0
    repositories = sorted(by_repo)
    while len(chosen) < count:
        progressed = False
        for repository in repositories:
            if round_index < len(by_repo[repository]):
                chosen.append(by_repo[repository][round_index])
                progressed = True
                if len(chosen) == count:
                    break
        if not progressed:
            break
        round_index += 1
    if len(chosen) != count:
        raise ValueError(f"could only select {len(chosen)} of {count} requested tasks")
    for ordinal, task in enumerate(chosen, start=1):
        task["ordinal"] = ordinal
    identity = "\n".join(task["instance_id"] for task in chosen)
    return {
        "schema_version": 1,
        "name": "repopilot-stage2-swebench-verified-medium-v1",
        "dataset": "SWE-bench/SWE-bench_Verified",
        "split": "test",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "selection_policy": {
            "difficulty": "15 min - 1 hour",
            "ordering": "sha256(seed:instance_id), round-robin by repository",
            "replacement_after_freeze": False,
            "excluded_development_instances": sorted(excluded),
        },
        "count": count,
        "instance_ids_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "instances": chosen,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", default="repopilot-stage2-swebench-verified-v1")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite frozen manifest: {args.output}")
    manifest = freeze(args.task_repo.resolve(), args.count, args.seed, set(args.exclude))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    print(manifest["instance_ids_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
