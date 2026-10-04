"""Append-only, hash-chained JSONL workflow journal."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from repopilot.core.models import JSONValue, Serializable, json_safe


class JournalCorruptionError(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowEvent(Serializable):
    sequence: int
    timestamp: str
    run_id: str
    event_type: str
    step_key: str
    payload: Dict[str, JSONValue]
    previous_checksum: str
    checksum: str


def _checksum(data: Dict[str, object]) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WorkflowJournal:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._thread_lock = threading.Lock()

    def replay(self) -> List[WorkflowEvent]:
        if not self.path.exists():
            return []
        events: List[WorkflowEvent] = []
        previous = ""
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines, start=1):
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines):
                    break
                raise JournalCorruptionError(f"invalid journal JSON at line {index}") from exc
            checksum = str(data.pop("checksum", ""))
            if data.get("run_id") != self.run_id:
                raise JournalCorruptionError(f"run id mismatch at line {index}")
            if int(data.get("sequence", 0)) != index:
                raise JournalCorruptionError(f"journal sequence gap at line {index}")
            if data.get("previous_checksum", "") != previous:
                raise JournalCorruptionError(f"journal checksum chain broken at line {index}")
            expected = _checksum(data)
            if checksum != expected:
                raise JournalCorruptionError(f"journal checksum mismatch at line {index}")
            previous = checksum
            events.append(WorkflowEvent(
                sequence=index,
                timestamp=str(data["timestamp"]),
                run_id=self.run_id,
                event_type=str(data["event_type"]),
                step_key=str(data.get("step_key", "")),
                payload=dict(data.get("payload", {})),
                previous_checksum=str(data.get("previous_checksum", "")),
                checksum=checksum,
            ))
        return events

    def append(self, event_type: str, *, step_key: str = "", **payload: object) -> WorkflowEvent:
        with self._thread_lock, self._lock_path.open("a+") as lock_handle:
            try:
                import fcntl
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover
                pass
            events = self.replay()
            sequence = len(events) + 1
            previous = events[-1].checksum if events else ""
            body: Dict[str, object] = {
                "sequence": sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "event_type": event_type,
                "step_key": step_key,
                "payload": json_safe(payload),
                "previous_checksum": previous,
            }
            digest = _checksum(body)
            body["checksum"] = digest
            line = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, (line + "\n").encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return WorkflowEvent(
                sequence, str(body["timestamp"]), self.run_id, event_type, step_key,
                dict(body["payload"]), previous, digest,  # type: ignore[arg-type]
            )
