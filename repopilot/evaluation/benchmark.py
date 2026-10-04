"""Provider-neutral benchmark task contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from repopilot.core.models import Serializable


@dataclass(frozen=True)
class BenchmarkTask(Serializable):
    id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_command: str
    gold_patch: Optional[str] = None
    expected_files: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "BenchmarkTask":
        task_id = str(data.get("id") or data.get("instance_id") or "").strip()
        repo = str(data.get("repo") or data.get("repository") or "").strip()
        problem = str(data.get("problem_statement") or data.get("task") or "").strip()
        test_command = str(data.get("test_command") or "").strip()
        base_commit = str(data.get("base_commit") or "HEAD").strip()
        if not task_id or not repo or not problem or not test_command:
            raise ValueError("benchmark task requires id, repo, problem_statement, and test_command")
        return cls(
            task_id, repo, base_commit, problem, test_command,
            str(data["gold_patch"]) if data.get("gold_patch") is not None else None,
            [str(item) for item in data.get("expected_files", [])],
        )


def load_benchmark(path: Path) -> List[BenchmarkTask]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("tasks", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("benchmark file must contain a list or a tasks list")
    tasks = [BenchmarkTask.from_dict(item) for item in rows]
    ids = [item.id for item in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark task ids must be unique")
    return tasks
