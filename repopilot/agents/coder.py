"""Coder role: prompt and tool selection only."""

from pathlib import Path

from repopilot.core.agent import Agent


class CoderAgent(Agent):
    TOOL_NAMES = [
        "glob", "read_file", "search_code", "write_file", "edit_file", "delete_file",
        "bash", "run_tests", "git_status", "git_diff",
    ]

    def __init__(self, prompt_path: Path = None) -> None:
        path = prompt_path or Path(__file__).resolve().parents[1] / "prompts" / "coder.md"
        super().__init__("coder", path.read_text(encoding="utf-8"), list(self.TOOL_NAMES))
