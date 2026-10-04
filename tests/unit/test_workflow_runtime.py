import asyncio
import json

import pytest

from repopilot.workflow import (
    JournalCorruptionError,
    WorkflowJournal,
    WorkflowRuntime,
    WorkflowStatus,
    WorkflowStep,
)


def test_workflow_resume_skips_completed_idempotent_step(tmp_path) -> None:
    calls = []

    async def first(context, state):
        calls.append((context.step_key, context.idempotency_key))
        return {"value": 1}

    async def failing(context, state):
        calls.append((context.step_key, context.idempotency_key))
        raise RuntimeError("crash")

    runtime = WorkflowRuntime(tmp_path / "run", "run-1", [
        WorkflowStep("first", first), WorkflowStep("second", failing),
    ])
    failed = asyncio.run(runtime.execute())
    assert failed.status is WorkflowStatus.FAILED
    assert failed.steps["first"].status is WorkflowStatus.COMPLETED

    async def repaired(context, state):
        calls.append((context.step_key, context.idempotency_key))
        return {"value": 2}

    resumed = WorkflowRuntime(tmp_path / "run", "run-1", [
        WorkflowStep("first", first), WorkflowStep("second", repaired),
    ])
    completed = asyncio.run(resumed.execute(resume=True))

    assert completed.status is WorkflowStatus.COMPLETED
    assert [key for key, _ in calls].count("first") == 1
    assert [key for key, _ in calls].count("second") == 2
    assert {value for _, value in calls} == {"run-1:first", "run-1:second"}


def test_journal_hash_chain_detects_tampering(tmp_path) -> None:
    journal = WorkflowJournal(tmp_path / "journal.jsonl", "run-1")
    journal.append("started")
    journal.append("finished")
    rows = journal.path.read_text().splitlines()
    item = json.loads(rows[0])
    item["event_type"] = "tampered"
    rows[0] = json.dumps(item)
    journal.path.write_text("\n".join(rows) + "\n")

    with pytest.raises(JournalCorruptionError, match="checksum"):
        journal.replay()
