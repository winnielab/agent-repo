"""Bounded, lease-backed execution of independent TaskGraph records."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from repopilot.agents.worker import WorkerResult
from repopilot.core.models import Serializable
from repopilot.observability.events import EventBus
from repopilot.task.graph import TaskGraph, TaskRecord, TaskStatus


@dataclass(frozen=True)
class WorkerAssignment(Serializable):
    run_id: str
    worker_id: str
    lease_owner: str
    task_id: str
    attempt: int


@dataclass
class CoordinatorResult(Serializable):
    run_id: str
    results: List[WorkerResult] = field(default_factory=list)
    failed_tasks: Dict[str, str] = field(default_factory=dict)
    max_parallelism: int = 0

    @property
    def success(self) -> bool:
        return not self.failed_tasks


WorkerCallable = Callable[[TaskRecord, WorkerAssignment], Awaitable[WorkerResult]]


class AgentCoordinator:
    """Claim ready tasks and execute them without sharing mutable worker state.

    The coordinator owns scheduling only. A worker factory is responsible for
    creating a fresh AgentRuntime, Context, ToolContext, and task worktree for
    each assignment. This keeps model/provider logic out of orchestration.
    """

    def __init__(self, worker: WorkerCallable, *, max_concurrency: int = 1,
                 lease_seconds: float = 300.0, run_id: Optional[str] = None,
                 event_bus: Optional[EventBus] = None) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.worker = worker
        self.max_concurrency = max_concurrency
        self.lease_seconds = lease_seconds
        self.run_id = run_id or uuid4().hex
        self.event_bus = event_bus or EventBus(self.run_id)

    async def _execute_one(self, graph: TaskGraph, record: TaskRecord,
                           assignment: WorkerAssignment) -> WorkerResult:
        graph.mark_running(record.id, assignment.lease_owner)
        self.event_bus.emit(
            "worker_started", task_id=record.id, worker_id=assignment.worker_id,
            attempt=assignment.attempt,
        )
        async def heartbeat() -> None:
            interval = max(0.05, min(self.lease_seconds / 3, 30.0))
            while True:
                await asyncio.sleep(interval)
                graph.renew_lease(
                    record.id, assignment.lease_owner, self.lease_seconds,
                )
                self.event_bus.emit(
                    "worker_lease_renewed", task_id=record.id,
                    worker_id=assignment.worker_id,
                )

        worker_future = asyncio.create_task(self.worker(record, assignment))
        heartbeat_future = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait(
                {worker_future, heartbeat_future}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_future in done:
                # A lost lease means this worker no longer has authority to publish.
                error = heartbeat_future.exception()
                worker_future.cancel()
                await asyncio.gather(worker_future, return_exceptions=True)
                raise RuntimeError(f"worker lease heartbeat failed: {error}")
            result = await worker_future
        except asyncio.CancelledError:
            worker_future.cancel()
            graph.mark_failed(record.id, "worker cancelled")
            self.event_bus.emit(
                "worker_finished", task_id=record.id, worker_id=assignment.worker_id,
                status="cancelled",
            )
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            graph.mark_failed(record.id, message)
            self.event_bus.emit(
                "worker_finished", task_id=record.id, worker_id=assignment.worker_id,
                status="failed", error=message,
            )
            return WorkerResult(record.id, "failed", "", 0, message)
        finally:
            heartbeat_future.cancel()
            await asyncio.gather(heartbeat_future, return_exceptions=True)

        if result.status in {"completed", "produced"}:
            if result.patch_sha256:
                graph.mark_produced(record.id, result.patch_sha256, result.result_commit or None)
            else:
                graph.mark_completed(record.id)
        else:
            graph.mark_failed(record.id, result.error or result.status)
        self.event_bus.emit(
            "worker_finished", task_id=record.id, worker_id=assignment.worker_id,
            status=result.status, patch_sha256=result.patch_sha256 or None,
        )
        return result

    async def execute(self, task_graph: TaskGraph) -> CoordinatorResult:
        result = CoordinatorResult(self.run_id)
        active: Dict[asyncio.Task[WorkerResult], WorkerAssignment] = {}
        self.event_bus.emit(
            "coordinator_started", max_concurrency=self.max_concurrency,
            task_count=len(task_graph.list()),
        )
        try:
            while True:
                while len(active) < self.max_concurrency:
                    worker_id = f"worker-{uuid4().hex[:8]}"
                    owner = f"{self.run_id}:{worker_id}"
                    record = task_graph.claim_ready(owner, self.lease_seconds)
                    if record is None:
                        break
                    assignment = WorkerAssignment(
                        self.run_id, worker_id, owner, record.id, record.attempt,
                    )
                    active[asyncio.create_task(
                        self._execute_one(task_graph, record, assignment)
                    )] = assignment
                    result.max_parallelism = max(result.max_parallelism, len(active))

                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for future in done:
                    assignment = active.pop(future)
                    worker_result = await future
                    result.results.append(worker_result)
                    if worker_result.status not in {"completed", "produced"}:
                        result.failed_tasks[assignment.task_id] = (
                            worker_result.error or worker_result.status
                        )
        finally:
            if active:
                for future in active:
                    future.cancel()
                await asyncio.gather(*active, return_exceptions=True)

        for record in task_graph.list():
            if record.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
                result.failed_tasks.setdefault(record.id, record.error or record.status.value)
        completed = sum(
            record.status in {TaskStatus.COMPLETED, TaskStatus.PRODUCED}
            for record in task_graph.list()
        )
        self.event_bus.emit(
            "coordinator_finished", success=result.success,
            completed=completed,
            failed=len(result.failed_tasks), max_parallelism=result.max_parallelism,
        )
        return result
