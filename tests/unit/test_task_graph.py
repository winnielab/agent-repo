import pytest

from repopilot.planning.plan import Plan, PlanStep
from repopilot.task.graph import TaskGraph, TaskStatus


def test_task_graph_persists_dependencies_and_ready_state(tmp_path) -> None:
    plan = Plan("plan", [
        PlanStep("a", "Implement", "change code"),
        PlanStep("b", "Verify", "verify behavior", ["a"]),
    ])
    graph = TaskGraph.from_plan(plan, tmp_path / "tasks")
    first = graph.ready_tasks()[0]
    graph.mark_running(first.id)
    graph.mark_completed(first.id)
    second = graph.ready_tasks()[0]
    assert second.title == "Verify"
    reloaded = TaskGraph(tmp_path / "tasks")
    assert reloaded.get(first.id).status is TaskStatus.COMPLETED


def test_cycle_and_failed_dependency_are_rejected_or_blocked(tmp_path) -> None:
    graph = TaskGraph(tmp_path / "tasks")
    first = graph.add("First", "one")
    second = graph.add("Second", "two", [first.id])
    with pytest.raises(ValueError, match="cycle"):
        graph.add_dependencies(first.id, [second.id])
    graph.mark_running(first.id)
    graph.mark_failed(first.id, "boom")
    assert graph.get(second.id).status is TaskStatus.BLOCKED
    assert graph.ready_tasks() == []

