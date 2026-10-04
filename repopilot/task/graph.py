"""File-persisted task DAG with validated state transitions."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set
from uuid import uuid4

from repopilot.core.models import Serializable
from repopilot.planning.plan import Plan


class TaskStatus(Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    PRODUCED = "produced"
    MERGING = "merging"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
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
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[float] = None
    attempt: int = 0
    workspace_id: Optional[str] = None
    base_commit: Optional[str] = None
    result_patch_sha256: Optional[str] = None
    result_commit: Optional[str] = None
    merge_status: Optional[str] = None
    verification_status: Optional[str] = None
    parent_task_id: Optional[str] = None
    agent_session_id: Optional[str] = None


class TaskGraph:
    ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._lock_path = self.directory / ".task-graph.lock"
        self._records: Dict[str, TaskRecord] = {}
        self._load()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialize graph mutations across threads and local processes."""
        with self._thread_lock:
            handle = self._lock_path.open("a+")
            try:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except ImportError:  # pragma: no cover - Windows fallback
                    pass
                yield
            finally:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except ImportError:  # pragma: no cover - Windows fallback
                    pass
                handle.close()

    def _path(self, task_id: str) -> Path:
        if not self.ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"invalid task ID: {task_id}")
        return self.directory / f"{task_id}.json"

    def _load(self) -> None:
        self._records.clear()
        for path in sorted(self.directory.glob("task_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            record = TaskRecord(
                id=data["id"], title=data["title"], description=data["description"],
                dependencies=list(data.get("dependencies", [])),
                status=TaskStatus(data.get("status", "pending")),
                owner=data.get("owner"), error=data.get("error"),
                lease_owner=data.get("lease_owner"),
                lease_expires_at=data.get("lease_expires_at"),
                attempt=int(data.get("attempt", 0)),
                workspace_id=data.get("workspace_id"),
                base_commit=data.get("base_commit"),
                result_patch_sha256=data.get("result_patch_sha256"),
                result_commit=data.get("result_commit"),
                merge_status=data.get("merge_status"),
                verification_status=data.get("verification_status"),
                parent_task_id=data.get("parent_task_id"),
                agent_session_id=data.get("agent_session_id"),
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

    def add(self, title: str, description: str, dependencies: Optional[List[str]] = None,
            *, parent_task_id: Optional[str] = None) -> TaskRecord:
        dependencies = list(dict.fromkeys(dependencies or []))
        if not title.strip() or not description.strip():
            raise ValueError("task title and description are required")
        with self._exclusive():
            self._load()
            if any(dep not in self._records for dep in dependencies):
                raise ValueError("dependency does not exist")
            if parent_task_id is not None and parent_task_id not in self._records:
                raise ValueError("parent task does not exist")
            for _ in range(100):
                task_id = "task_" + uuid4().hex[:8]
                if task_id not in self._records:
                    break
            else:
                raise RuntimeError("could not allocate task ID")
            record = TaskRecord(
                task_id, title.strip(), description.strip(), dependencies,
                parent_task_id=parent_task_id,
            )
            self._records[task_id] = record
            try:
                self._validate_graph()
                self._save(record)
            except Exception:
                self._records.pop(task_id, None)
                raise
        return record

    def add_dependencies(self, task_id: str, dependencies: List[str]) -> TaskRecord:
        additions = list(dict.fromkeys(dependencies))
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status is not TaskStatus.PENDING or record.owner is not None:
                raise ValueError("dependencies can only change for an unowned pending task")
            if task_id in additions or any(dep not in self._records for dep in additions):
                raise ValueError("invalid dependency")
            original = list(record.dependencies)
            record.dependencies.extend(
                dep for dep in additions if dep not in record.dependencies
            )
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
                if record.status in {TaskStatus.PENDING, TaskStatus.RETRYABLE}
                and all(self._records[dep].status is TaskStatus.COMPLETED for dep in record.dependencies)]

    def claim_ready(self, owner: str, lease_seconds: float = 300.0,
                    now: Optional[float] = None) -> Optional[TaskRecord]:
        """Atomically claim one ready task and persist its renewable lease."""
        if not owner.strip():
            raise ValueError("lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed_at = time.time() if now is None else now
        with self._exclusive():
            self._load()
            self._recover_expired_leases(claimed_at)
            ready = self.ready_tasks()
            if not ready:
                return None
            record = ready[0]
            record.status = TaskStatus.CLAIMED
            record.owner = owner
            record.lease_owner = owner
            record.lease_expires_at = claimed_at + lease_seconds
            record.attempt += 1
            record.error = None
            self._save(record)
            return record

    def renew_lease(self, task_id: str, owner: str, lease_seconds: float = 300.0,
                    now: Optional[float] = None) -> TaskRecord:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.lease_owner != owner or record.status not in {
                TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.PRODUCED,
                TaskStatus.MERGING, TaskStatus.VERIFYING,
            }:
                raise ValueError(f"task lease is not owned by {owner}: {task_id}")
            record.lease_expires_at = (time.time() if now is None else now) + lease_seconds
            self._save(record)
            return record

    def _recover_expired_leases(self, now: float) -> List[TaskRecord]:
        recovered: List[TaskRecord] = []
        for record in self._records.values():
            if (record.status in {TaskStatus.CLAIMED, TaskStatus.RUNNING}
                    and record.lease_expires_at is not None
                    and record.lease_expires_at <= now):
                record.status = TaskStatus.RETRYABLE
                record.owner = None
                record.lease_owner = None
                record.lease_expires_at = None
                record.error = "worker lease expired"
                self._save(record)
                recovered.append(record)
        return recovered

    def recover_expired_leases(self, now: Optional[float] = None) -> List[TaskRecord]:
        with self._exclusive():
            self._load()
            return self._recover_expired_leases(time.time() if now is None else now)

    def mark_running(self, task_id: str, owner: str = "coder") -> TaskRecord:
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status is TaskStatus.CLAIMED:
                if record.lease_owner != owner:
                    raise ValueError(f"task lease is not owned by {owner}: {task_id}")
            elif (record.status not in {TaskStatus.PENDING, TaskStatus.RETRYABLE}
                  or record not in self.ready_tasks()):
                raise ValueError(f"task is not ready: {task_id}")
            record.status, record.owner, record.error = TaskStatus.RUNNING, owner, None
            self._save(record)
            return record

    def bind_workspace(self, task_id: str, owner: str, workspace_id: str,
                       base_commit: str, agent_session_id: Optional[str] = None) -> TaskRecord:
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status not in {TaskStatus.CLAIMED, TaskStatus.RUNNING}:
                raise ValueError(f"task is not claimed or running: {task_id}")
            if record.lease_owner and record.lease_owner != owner:
                raise ValueError(f"task lease is not owned by {owner}: {task_id}")
            record.workspace_id = workspace_id
            record.base_commit = base_commit
            record.agent_session_id = agent_session_id
            self._save(record)
            return record

    def mark_produced(self, task_id: str, patch_sha256: str,
                      result_commit: Optional[str] = None) -> TaskRecord:
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status is not TaskStatus.RUNNING:
                raise ValueError(f"task is not running: {task_id}")
            record.status = TaskStatus.PRODUCED
            record.result_patch_sha256 = patch_sha256
            record.result_commit = result_commit
            self._save(record)
            return record

    def mark_merging(self, task_id: str) -> TaskRecord:
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status is not TaskStatus.PRODUCED:
                raise ValueError(f"task has not produced an artifact: {task_id}")
            record.status = TaskStatus.MERGING
            record.merge_status = "running"
            self._save(record)
            return record

    def mark_verifying(self, task_id: str,
                       result_commit: Optional[str] = None) -> TaskRecord:
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status not in {TaskStatus.PRODUCED, TaskStatus.MERGING}:
                raise ValueError(f"task is not ready for verification: {task_id}")
            record.status = TaskStatus.VERIFYING
            record.merge_status = "completed"
            record.verification_status = "running"
            if result_commit is not None:
                record.result_commit = result_commit
            self._save(record)
            return record

    def mark_completed(self, task_id: str) -> TaskRecord:
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status not in {
                TaskStatus.RUNNING, TaskStatus.PRODUCED, TaskStatus.VERIFYING,
            }:
                raise ValueError(f"task is not running: {task_id}")
            record.status, record.error = TaskStatus.COMPLETED, None
            if record.verification_status == "running":
                record.verification_status = "passed"
            record.lease_owner = None
            record.lease_expires_at = None
            self._save(record)
            return record

    def mark_failed(self, task_id: str, error: str) -> TaskRecord:
        with self._exclusive():
            self._load()
            record = self.get(task_id)
            if record.status not in {
                TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.PRODUCED,
                TaskStatus.MERGING, TaskStatus.VERIFYING,
            }:
                raise ValueError(f"task is not running: {task_id}")
            record.status, record.error = TaskStatus.FAILED, error
            if record.merge_status == "running":
                record.merge_status = "failed"
            if record.verification_status == "running":
                record.verification_status = "failed"
            record.lease_owner = None
            record.lease_expires_at = None
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
