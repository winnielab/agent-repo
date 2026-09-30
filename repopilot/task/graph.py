"""File-persisted task DAG with validated state transitions."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set
from uuid import uuid4

from repopilot.core.models import Serializable
from repopilot.planning.plan import Plan


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class TaskRecord(Serializable):
    id: str
    title: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    owner: Optional[str] = None
    error: Optional[str] = None


class TaskGraph:
    ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._records: Dict[str, TaskRecord] = {}
        self._load()

    def _path(self, task_id: str) -> Path:
        if not self.ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"invalid task ID: {task_id}")
        return self.directory / f"{task_id}.json"

    def _load(self) -> None:
        for path in sorted(self.directory.glob("task_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            record = TaskRecord(
                id=data["id"], title=data["title"], description=data["description"],
                dependencies=list(data.get("dependencies", [])),
                status=TaskStatus(data.get("status", "pending")),
                owner=data.get("owner"), error=data.get("error"),
            )
            if path != self._path(record.id):
                raise ValueError(f"task file name does not match ID: {path}")
            self._records[record.id] = record
        self._validate_graph()

    def _save(self, record: TaskRecord) -> None:
        path = self._path(record.id)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _validate_graph(self) -> None:
        known = set(self._records)
        if any(dep not in known for record in self._records.values() for dep in record.dependencies):
            raise ValueError("task graph references a missing dependency")
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task graph contains a dependency cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in self._records[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in known:
            visit(task_id)

    def add(self, title: str, description: str, dependencies: Optional[List[str]] = None) -> TaskRecord:
        dependencies = list(dict.fromkeys(dependencies or []))
        if not title.strip() or not description.strip():
            raise ValueError("task title and description are required")
        if any(dep not in self._records for dep in dependencies):
            raise ValueError("dependency does not exist")
        for _ in range(100):
            task_id = "task_" + uuid4().hex[:8]
            if task_id not in self._records:
                break
        else:
            raise RuntimeError("could not allocate task ID")
        record = TaskRecord(task_id, title.strip(), description.strip(), dependencies)
        self._records[task_id] = record
        try:
            self._validate_graph()
            self._save(record)
        except Exception:
            self._records.pop(task_id, None)
            raise
        return record

    def add_dependencies(self, task_id: str, dependencies: List[str]) -> TaskRecord:
        record = self.get(task_id)
        if record.status is not TaskStatus.PENDING or record.owner is not None:
            raise ValueError("dependencies can only change for an unowned pending task")
        additions = list(dict.fromkeys(dependencies))
        if task_id in additions or any(dep not in self._records for dep in additions):
            raise ValueError("invalid dependency")
        original = list(record.dependencies)
        record.dependencies.extend(dep for dep in additions if dep not in record.dependencies)
        try:
            self._validate_graph()
            self._save(record)
        except Exception:
            record.dependencies = original
            raise
        return record

    @classmethod
    def from_plan(cls, plan: Plan, directory: Path) -> "TaskGraph":
        graph = cls(directory)
        if graph._records:
            return graph
        mapping: Dict[str, str] = {}
        # Create in dependency-ready passes while preserving plan order.
        pending = list(plan.steps)
        while pending:
            progressed = False
            for step in list(pending):
                if all(dep in mapping for dep in step.dependencies):
                    record = graph.add(step.title, step.description,
                                       [mapping[dep] for dep in step.dependencies])
                    mapping[step.key] = record.id
                    pending.remove(step)
                    progressed = True
            if not progressed:
                raise ValueError("plan dependencies contain a cycle")
        return graph

    def list(self) -> List[TaskRecord]:
        return list(self._records.values())

    def get(self, task_id: str) -> TaskRecord:
        return self._records[task_id]

    def ready_tasks(self) -> List[TaskRecord]:
        return [record for record in self._records.values()
                if record.status is TaskStatus.PENDING
                and all(self._records[dep].status is TaskStatus.COMPLETED for dep in record.dependencies)]

    def mark_running(self, task_id: str, owner: str = "coder") -> TaskRecord:
        record = self.get(task_id)
        if record.status is not TaskStatus.PENDING or record not in self.ready_tasks():
            raise ValueError(f"task is not ready: {task_id}")
        record.status, record.owner, record.error = TaskStatus.RUNNING, owner, None
        self._save(record)
        return record

    def mark_completed(self, task_id: str) -> TaskRecord:
        record = self.get(task_id)
        if record.status is not TaskStatus.RUNNING:
            raise ValueError(f"task is not running: {task_id}")
        record.status, record.error = TaskStatus.COMPLETED, None
        self._save(record)
        return record

    def mark_failed(self, task_id: str, error: str) -> TaskRecord:
        record = self.get(task_id)
        if record.status is not TaskStatus.RUNNING:
            raise ValueError(f"task is not running: {task_id}")
        record.status, record.error = TaskStatus.FAILED, error
        self._save(record)
        self._propagate_blocked()
        return record

    def _propagate_blocked(self) -> None:
        changed = True
        while changed:
            changed = False
            for record in self._records.values():
                if record.status is TaskStatus.PENDING and any(
                    self._records[dep].status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
                    for dep in record.dependencies
                ):
                    record.status = TaskStatus.BLOCKED
                    record.error = "dependency failed or is blocked"
                    self._save(record)
                    changed = True

    @property
    def complete(self) -> bool:
        return bool(self._records) and all(record.status is TaskStatus.COMPLETED for record in self._records.values())
