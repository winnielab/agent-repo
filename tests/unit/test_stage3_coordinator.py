import asyncio

from repopilot.agents.worker import WorkerResult
from repopilot.orchestration.coordinator import AgentCoordinator
from repopilot.task.graph import TaskGraph, TaskStatus


def test_task_claim_lease_is_persisted_and_expired_lease_is_retryable(tmp_path) -> None:
    graph = TaskGraph(tmp_path / "tasks")
    task = graph.add("Implement", "change code")

    claimed = graph.claim_ready("run:worker", lease_seconds=10, now=100)
    assert claimed is not None
    assert claimed.status is TaskStatus.CLAIMED
    assert claimed.attempt == 1
    assert TaskGraph(tmp_path / "tasks").get(task.id).lease_owner == "run:worker"

    recovered = graph.recover_expired_leases(now=111)
    assert [item.id for item in recovered] == [task.id]
    assert graph.get(task.id).status is TaskStatus.RETRYABLE
    claimed_again = graph.claim_ready("run:worker-2", lease_seconds=10, now=112)
    assert claimed_again is not None
    assert claimed_again.attempt == 2


def test_stale_graph_instance_reloads_before_state_transition(tmp_path) -> None:
    directory = tmp_path / "tasks"
    first = TaskGraph(directory)
    task = first.add("Implement", "change code")
    stale = TaskGraph(directory)

    claimed = first.claim_ready("run:worker", lease_seconds=30, now=100)
    assert claimed is not None
    running = stale.mark_running(task.id, "run:worker")

    assert running.status is TaskStatus.RUNNING
    assert running.lease_owner == "run:worker"
    assert running.lease_expires_at == 130
    persisted = TaskGraph(directory).get(task.id)
    assert persisted.lease_owner == "run:worker"
    assert persisted.attempt == 1


def test_coordinator_runs_independent_workers_concurrently_and_isolates_failure(tmp_path) -> None:
    graph = TaskGraph(tmp_path / "tasks")
    graph.add("A", "first")
    graph.add("B", "second")
    active = 0
    maximum = 0

    async def worker(record, assignment):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        if record.title == "B":
            raise RuntimeError("independent failure")
        return WorkerResult(record.id, "completed", assignment.worker_id, 1)

    result = asyncio.run(AgentCoordinator(
        worker, max_concurrency=2, run_id="run",
    ).execute(graph))

    assert maximum == result.max_parallelism == 2
    assert len(result.results) == 2
    assert len(result.failed_tasks) == 1
    states = {record.title: record.status for record in graph.list()}
    assert states == {"A": TaskStatus.COMPLETED, "B": TaskStatus.FAILED}


def test_coordinator_publishes_partial_patch_after_worker_limit(tmp_path) -> None:
    graph = TaskGraph(tmp_path / "tasks")
    task = graph.add("A", "change a")

    async def worker(record, assignment):
        return WorkerResult(
            record.id, "produced", "token limit after edit", 3,
            "runtime limit reached: max_tokens",
            patch_sha256="a" * 64,
        )

    result = asyncio.run(AgentCoordinator(worker, run_id="run").execute(graph))

    assert result.failed_tasks == {}
    assert graph.get(task.id).status is TaskStatus.PRODUCED
    assert graph.get(task.id).result_patch_sha256 == "a" * 64
