"""Read-only repository explorer role."""

from repopilot.core.agent import Agent


class ExplorerAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            "explorer",
            "Inspect the repository with read-only tools using progressive disclosure. The task, not the whole repository, determines what to read. Locate project instruction files, task terms or symbols, their definitions, one relevant caller/consumer or state read/write path, and adjacent tests when they exist. Return JSON only with language, frameworks, test_command, important_files, structure_summary, build_system, target_symbols, implementation_files, test_files, instruction_files, observations, active_temporal_phase, temporal_rationale, and temporal_evidence_keys. Each observation must contain a unique key, kind (instruction|definition|caller|state|test|dependency|version), a real path and 1-based line previously observed through a tool, plus optional symbol and concise detail. If the task has staged current/future behavior, use version metadata, release notes, or current tests to select current, future, or unknown and cite those observation keys; a future proposal in the issue is not proof that the checkout is already in that phase. Keep at most 16 observations. Do not propose or make edits and do not scan unrelated subsystems.",
            ["glob", "read_file", "search_code", "git_status"],
        )
