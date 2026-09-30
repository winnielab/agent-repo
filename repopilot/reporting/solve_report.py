"""Machine-readable and concise Markdown solve reports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from repopilot.core.models import AgentResult, json_safe


@dataclass
class SolveReport:
    status: str
    task: str
    run_id: str
    workspace: Optional[str]
    iterations: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    runtime_seconds: float
    tests: Dict[str, Any]
    changed_files: List[str]
    diff_path: Optional[str]
    event_log: str
    final_message: str
    error: Optional[str] = None

    @classmethod
    def from_agent(
        cls,
        agent: AgentResult,
        task: str,
        run_id: str,
        workspace: Optional[Path],
        tool_calls: int,
        runtime_seconds: float,
        tests: Dict[str, Any],
        diff_path: Optional[Path],
        event_log: Path,
    ) -> "SolveReport":
        return cls(
            status=agent.status,
            task=task,
            run_id=run_id,
            workspace=str(workspace) if workspace else None,
            iterations=agent.iterations,
            tool_calls=tool_calls,
            input_tokens=agent.usage.input_tokens,
            output_tokens=agent.usage.output_tokens,
            runtime_seconds=runtime_seconds,
            tests=tests,
            changed_files=agent.changed_files,
            diff_path=str(diff_path) if diff_path else None,
            event_log=str(event_log),
            final_message=agent.final_text,
            error=agent.error,
        )

    def to_dict(self) -> Dict[str, Any]:
        return json_safe(self)  # type: ignore[return-value]

    def write(self, directory: Path) -> tuple[Path, Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "solve-report.json"
        markdown_path = directory / "solve-report.md"
        json_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = [
            "# RepoPilot Solve Report", "",
            f"- Status: `{self.status}`",
            f"- Run: `{self.run_id}`",
            f"- Iterations: {self.iterations}",
            f"- Tool calls: {self.tool_calls}",
            f"- Tokens: {self.input_tokens} input / {self.output_tokens} output",
            f"- Runtime: {self.runtime_seconds:.3f}s",
            f"- Changed files: {', '.join(self.changed_files) or '(none)'}",
            "", "## Task", "", self.task,
            "", "## Final message", "", self.final_message or "(none)",
        ]
        if self.error:
            lines.extend(["", "## Error", "", self.error])
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return json_path, markdown_path
