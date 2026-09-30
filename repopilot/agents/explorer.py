"""Read-only repository explorer role."""

from repopilot.core.agent import Agent


class ExplorerAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            "explorer",
            "Inspect the repository with read-only tools. Return JSON only with language, frameworks, test_command, important_files, structure_summary, and build_system. Do not propose or make edits.",
            ["glob", "read_file", "search_code", "git_status"],
        )

