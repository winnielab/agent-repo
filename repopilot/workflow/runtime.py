"""Idempotent step runtime with checkpoints, journal replay, and a resume lock."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterator, List, Union

from repopilot.core.models import JSONValue, Serializable, json_safe
from repopilot.workflow.checkpoint import CheckpointStore
from repopilot.workflow.journal import WorkflowJournal
from repopilot.workflow.state import StepState, WorkflowState, WorkflowStatus


class WorkflowLockedError(RuntimeError):
    pass


@dataclass(frozen=True)
class StepContext(Serializable):
    run_id: str
    step_key: str
    attempt: int
    idempotency_key: str
    run_directory: Path


StepHandler = Callable[[StepContext, WorkflowState], Union[JSONValue, Awaitable[JSONValue]]]


@dataclass(frozen=True)
class WorkflowStep:
    key: str
    handler: StepHandler


class WorkflowRuntime:
    def __init__(self, run_directory: Path, run_id: str,
                 steps: List[WorkflowStep]) -> None:
        self.run_directory = Path(run_directory)
        self.run_directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        keys = [step.key for step in steps]
        if not keys or any(not key.strip() for key in keys):
            raise ValueError("workflow steps require non-empty keys")
        if len(keys) != len(set(keys)):
            raise ValueError("workflow step keys must be unique")
        self.steps = steps
        self.handlers: Dict[str, StepHandler] = {step.key: step.handler for step in steps}
        self.journal = WorkflowJournal(self.run_directory / "journal.jsonl", run_id)
        self.checkpoints = CheckpointStore(self.run_directory / "snapshot.json")
        self.lock_path = self.run_directory / "resume.lock"

    @contextmanager
    def _run_lock(self) -> Iterator[None]:
        handle = self.lock_path.open("a+")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkflowLockedError(f"workflow run is already active: {self.run_id}") from exc
            except ImportError:  # pragma: no cover
                pass
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            handle.close()

    def _new_state(self) -> WorkflowState:
        return WorkflowState(
            self.run_id,
            steps={step.key: StepState(step.key) for step in self.steps},
        )

    def load_state(self) -> WorkflowState:
        state = self.checkpoints.load() or self._new_state()
        if state.run_id != self.run_id:
            raise ValueError("checkpoint run id does not match runtime")
        events = self.journal.replay()
        if state.journal_sequence > len(events):
            raise ValueError("checkpoint points beyond the durable journal")
        if state.journal_sequence:
            expected = events[state.journal_sequence - 1].checksum
            if state.journal_checksum != expected:
                raise ValueError("checkpoint journal checksum does not match")
        for step in self.steps:
            state.steps.setdefault(step.key, StepState(step.key))
        unknown = set(state.steps) - set(self.handlers)
        if unknown:
            raise ValueError("checkpoint contains unknown workflow steps: " + ", ".join(sorted(unknown)))
        return state

    def _persist_event(self, state: WorkflowState, event_type: str,
                       *, step_key: str = "", **payload: object) -> None:
        event = self.journal.append(event_type, step_key=step_key, **payload)
        state.journal_sequence = event.sequence
        state.journal_checksum = event.checksum
        self.checkpoints.save(state)

    async def execute(self, *, resume: bool = False) -> WorkflowState:
        with self._run_lock():
            state = self.load_state()
            if state.status is WorkflowStatus.COMPLETED:
                return state
            if not resume and state.journal_sequence:
                raise ValueError(f"workflow run already exists: {self.run_id}")
            if resume:
                for step_state in state.steps.values():
                    if step_state.status is WorkflowStatus.RUNNING:
                        step_state.status = WorkflowStatus.INTERRUPTED
                        self._persist_event(
                            state, "step_interrupted", step_key=step_state.key,
                            attempt=step_state.attempt,
                        )
                        step_state.status = WorkflowStatus.PENDING
            state.status = WorkflowStatus.RUNNING
            state.error = None
            self._persist_event(state, "workflow_resumed" if resume else "workflow_started")

            for step in self.steps:
                step_state = state.steps[step.key]
                if step_state.status is WorkflowStatus.COMPLETED:
                    continue
                step_state.status = WorkflowStatus.RUNNING
                step_state.attempt += 1
                step_state.error = None
                state.current_step = step.key
                context = StepContext(
                    self.run_id, step.key, step_state.attempt,
                    f"{self.run_id}:{step.key}", self.run_directory,
                )
                self._persist_event(
                    state, "step_started", step_key=step.key,
                    attempt=step_state.attempt, idempotency_key=context.idempotency_key,
                )
                try:
                    output = step.handler(context, state)
                    if inspect.isawaitable(output):
                        output = await output
                    step_state.output = json_safe(output)
                    step_state.status = WorkflowStatus.COMPLETED
                    if step.key not in state.completed_order:
                        state.completed_order.append(step.key)
                    self._persist_event(
                        state, "step_completed", step_key=step.key,
                        attempt=step_state.attempt, output=step_state.output,
                    )
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    step_state.status = WorkflowStatus.FAILED
                    step_state.error = message
                    state.status = WorkflowStatus.FAILED
                    state.error = message
                    self._persist_event(
                        state, "step_failed", step_key=step.key,
                        attempt=step_state.attempt, error=message,
                    )
                    return state

            state.current_step = None
            state.status = WorkflowStatus.COMPLETED
            self._persist_event(state, "workflow_completed")
            return state
