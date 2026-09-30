"""Small JSON-persisted working memory; not a transcript or vector store."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set

from repopilot.core.models import json_safe


@dataclass
class WorkingMemory:
    path: Path
    relevant_files: Set[str] = field(default_factory=set)
    hypotheses: List[str] = field(default_factory=list)
    failed_attempts: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    test_failures: List[str] = field(default_factory=list)
    invalidated_assumptions: List[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "WorkingMemory":
        path = Path(path)
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = {
            "relevant_files", "hypotheses", "failed_attempts", "decisions",
            "test_failures", "invalidated_assumptions",
        }
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("invalid working-memory schema")
        return cls(
            path,
            set(data.get("relevant_files", [])),
            list(data.get("hypotheses", [])),
            list(data.get("failed_attempts", [])),
            list(data.get("decisions", [])),
            list(data.get("test_failures", [])),
            list(data.get("invalidated_assumptions", [])),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "relevant_files": sorted(self.relevant_files),
            "hypotheses": self.hypotheses,
            "failed_attempts": self.failed_attempts,
            "decisions": self.decisions,
            "test_failures": self.test_failures,
            "invalidated_assumptions": self.invalidated_assumptions,
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def remember(self, category: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        if category == "relevant_files":
            self.relevant_files.add(value)
        else:
            collection = getattr(self, category)
            if value not in collection:
                collection.append(value)
        self.save()

    def prompt_context(self) -> str:
        return json.dumps({
            "relevant_files": sorted(self.relevant_files),
            "hypotheses": self.hypotheses[-10:],
            "failed_attempts": self.failed_attempts[-10:],
            "decisions": self.decisions[-10:],
            "test_failures": self.test_failures[-10:],
            "invalidated_assumptions": self.invalidated_assumptions[-10:],
        }, ensure_ascii=False, indent=2)
