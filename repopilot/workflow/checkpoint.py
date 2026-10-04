"""Atomic workflow snapshots that point into the append-only journal."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from repopilot.workflow.state import WorkflowState


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, state: WorkflowState) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with temporary.open("r+", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def load(self) -> Optional[WorkflowState]:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return WorkflowState.from_dict(data)
