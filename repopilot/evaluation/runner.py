"""Resumable evaluation runner whose reports derive only from task records."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from repopilot.core.models import Serializable
from repopilot.evaluation.benchmark import BenchmarkTask


@dataclass
class EvaluationRecord(Serializable):
    task_id: str
    success: bool
    patch_applied: bool = False
    tests_passed: bool = False
    regression_passed: bool = False
    iterations: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Optional[float] = None
    runtime_seconds: float = 0.0
    changed_files: List[str] = field(default_factory=list)
    expected_files: List[str] = field(default_factory=list)
    human_intervention: bool = False
    error: Optional[str] = None
    variant: str = "full"

    @property
    def localization_accuracy(self) -> Optional[float]:
        expected = set(self.expected_files)
        if not expected:
            return None
        changed = set(self.changed_files)
        return len(expected & changed) / len(expected)

    @classmethod
    def from_dict(cls, data: dict) -> "EvaluationRecord":
        fields = {
            key: value for key, value in data.items()
            if key in cls.__dataclass_fields__
        }
        return cls(**fields)


EvaluationCallable = Callable[[BenchmarkTask, str], Awaitable[EvaluationRecord]]


class EvaluationRunner:
    def __init__(self, output_dir: Path, evaluator: EvaluationCallable,
                 *, max_concurrency: int = 1) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.evaluator = evaluator
        self.max_concurrency = max_concurrency
        self.records_dir = self.output_dir / "records"
        self.records_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, task_id: str, variant: str) -> Path:
        safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in task_id)
        safe_variant = "".join(
            char if char.isalnum() or char in "._-" else "-" for char in variant
        )
        identity = hashlib.sha256(f"{task_id}\0{variant}".encode("utf-8")).hexdigest()[:10]
        return self.records_dir / f"{safe[:80]}--{safe_variant[:40]}--{identity}.json"

    def _save_record(self, record: EvaluationRecord) -> None:
        path = self._record_path(record.task_id, record.variant)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    async def _run_one(self, task: BenchmarkTask, variant: str,
                       semaphore: asyncio.Semaphore, resume: bool) -> EvaluationRecord:
        path = self._record_path(task.id, variant)
        if resume and path.exists():
            return EvaluationRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        async with semaphore:
            started = time.monotonic()
            try:
                record = await self.evaluator(task, variant)
                if record.task_id != task.id:
                    raise ValueError("evaluator returned a mismatched task id")
                record.variant = variant
                if not record.expected_files:
                    record.expected_files = list(task.expected_files)
                if not record.runtime_seconds:
                    record.runtime_seconds = time.monotonic() - started
            except Exception as exc:
                record = EvaluationRecord(
                    task.id, False, runtime_seconds=time.monotonic() - started,
                    expected_files=list(task.expected_files),
                    error=f"{type(exc).__name__}: {exc}", variant=variant,
                )
            self._save_record(record)
            return record

    async def run(self, tasks: List[BenchmarkTask], *, variant: str = "full",
                  resume: bool = True) -> List[EvaluationRecord]:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        records = await asyncio.gather(*[
            self._run_one(task, variant, semaphore, resume) for task in tasks
        ])
        self.write_reports(records)
        return records

    @staticmethod
    def metrics(records: List[EvaluationRecord]) -> Dict[str, object]:
        count = len(records)
        if not count:
            return {"tasks": 0, "resolve_rate": 0.0}
        localizations = [
            value for value in (record.localization_accuracy for record in records)
            if value is not None
        ]
        costs = [record.cost for record in records if record.cost is not None]
        return {
            "tasks": count,
            "resolved": sum(record.success for record in records),
            "resolve_rate": sum(record.success for record in records) / count,
            "non_empty_patch_count": sum(record.patch_applied for record in records),
            "non_empty_patch_rate": sum(record.patch_applied for record in records) / count,
            "test_pass_rate": sum(record.tests_passed for record in records) / count,
            "candidate_test_pass_rate": sum(
                record.tests_passed and record.patch_applied for record in records
            ) / count,
            "regression_pass_rate": sum(record.regression_passed for record in records) / count,
            "average_iterations": sum(record.iterations for record in records) / count,
            "average_tool_calls": sum(record.tool_calls for record in records) / count,
            "average_tokens": sum(
                record.input_tokens + record.output_tokens for record in records
            ) / count,
            "average_cost": sum(costs) / len(costs) if costs else None,
            "average_runtime_seconds": sum(record.runtime_seconds for record in records) / count,
            "human_intervention_rate": sum(record.human_intervention for record in records) / count,
            "localization_accuracy": (
                sum(localizations) / len(localizations) if localizations else None
            ),
        }

    def write_reports(self, records: List[EvaluationRecord]) -> None:
        metrics = self.metrics(records)
        (self.output_dir / "summary.json").write_text(
            json.dumps(metrics, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with (self.output_dir / "tasks.jsonl").open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        with (self.output_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["metric", "value"])
            writer.writerows(metrics.items())
        lines = ["# RepoPilot Evaluation Report", "", f"Tasks: {metrics['tasks']}", ""]
        for key, value in metrics.items():
            if key == "tasks":
                continue
            lines.append(f"- {key}: {value}")
        lines.extend([
            "", "## Task Results", "",
            "| Task | Variant | Resolved | Tests | Runtime (s) |",
            "|---|---|---:|---:|---:|",
        ])
        lines.extend(
            f"| {record.task_id} | {record.variant} | {int(record.success)} | "
            f"{int(record.tests_passed)} | {record.runtime_seconds:.3f} |"
            for record in records
        )
        (self.output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
