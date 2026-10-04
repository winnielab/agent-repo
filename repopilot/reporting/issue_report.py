"""Complete Stage 2 issue-solving report."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from repopilot.core.models import json_safe


@dataclass
class IssueSolveReport:
    status: str
    run_id: str
    issue: str
    specification: Dict[str, Any]
    repository: Dict[str, Any]
    plan: Dict[str, Any]
    tasks: List[Dict[str, Any]]
    workers: List[Dict[str, Any]]
    tests: Dict[str, Any]
    reviewer: Dict[str, Any]
    evaluation: Dict[str, Any]
    tests_passed: bool
    regression_passed: bool
    repair_iterations: int
    iterations: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost: Optional[float]
    runtime_seconds: float
    changed_files: List[str]
    diff_path: Optional[str]
    event_log: str
    workspace: Optional[str]
    error: Optional[str] = None
    orchestration: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return json_safe(self)  # type: ignore[return-value]

    def write(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "issue-report.json"
        markdown_path = directory / "issue-report.md"
        json_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = [
            "# RepoPilot Issue Solve Report", "",
            f"- Status: `{self.status}`", f"- Run: `{self.run_id}`",
            f"- Tasks: {sum(item['status'] == 'completed' for item in self.tasks)}/{len(self.tasks)} completed",
            f"- Repairs: {self.repair_iterations}", f"- Agent iterations: {self.iterations}",
            f"- Tool calls: {self.tool_calls}",
            f"- Tokens: {self.input_tokens} input / {self.output_tokens} output",
            f"- Cost: {self.cost if self.cost is not None else 'unavailable'}",
            f"- Runtime: {self.runtime_seconds:.3f}s",
            f"- Tests passed: `{self.tests_passed}`",
            f"- Regression suite passed: `{self.regression_passed}`",
            f"- Reviewer: `{self.reviewer.get('decision', 'unavailable')}`",
            f"- Goal evaluation: `{'pass' if self.evaluation.get('passed') else 'fail'}`",
            "", "## Issue", "", self.issue,
            "", "## Acceptance criteria", "",
        ]
        if self.orchestration:
            lines.insert(4, f"- Orchestration: `{self.orchestration.get('mode', 'unknown')}`")
        lines.extend(f"- {item['status'].upper()}: {item['criterion']} — {item['evidence']}"
                     for item in self.evaluation.get("criteria", []))
        if self.error:
            lines.extend(["", "## Error", "", self.error])
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return json_path, markdown_path
