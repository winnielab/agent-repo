"""Configuration for a single RepoPilot solve run."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass(frozen=True)
class RepoPilotConfig:
    max_iterations: int = 30
    max_tool_calls: int = 120
    max_tokens: int = 200_000
    finalization_token_reserve: int = 30_000
    repair_token_reserve: int = 40_000
    evaluation_token_reserve: int = 20_000
    max_post_test_iterations: int = 1
    max_no_mutation_iterations: int = 8
    max_explorer_iterations: int = 12
    max_explorer_tool_calls: int = 40
    max_explorer_tokens: int = 120_000
    execution_image: Optional[str] = None
    execution_platform: Optional[str] = None
    execution_setup_command: str = ""
    max_runtime_seconds: float = 120.0
    max_repair_iterations: int = 3
    max_cost: Optional[float] = None
    command_timeout_seconds: float = 30.0
    max_command_output_chars: int = 50_000
    context_char_budget: int = 80_000
    large_tool_result_chars: int = 12_000
    baseline_aware_tests: bool = False
    hydrate_runtime_artifacts: bool = True
    max_runtime_artifact_files: int = 4096
    max_runtime_artifact_bytes: int = 1_073_741_824
    test_command: str = "python -m pytest -q"
    worktree_parent: Optional[Path] = None
    keep_worktree: bool = True
    allowed_command_prefixes: Tuple[str, ...] = (
        "ls", "pwd", "find", "rg", "grep", "cat", "head", "tail", "sed",
        "wc", "file", "python", "python3", "pytest", "git",
    )

    def validate(self) -> None:
        numeric = {
            "max_iterations": self.max_iterations,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "max_explorer_iterations": self.max_explorer_iterations,
            "max_explorer_tool_calls": self.max_explorer_tool_calls,
            "max_explorer_tokens": self.max_explorer_tokens,
            "max_runtime_seconds": self.max_runtime_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
            "context_char_budget": self.context_char_budget,
            "max_runtime_artifact_files": self.max_runtime_artifact_files,
            "max_runtime_artifact_bytes": self.max_runtime_artifact_bytes,
        }
        invalid = [name for name, value in numeric.items() if value <= 0]
        if invalid:
            raise ValueError("configuration values must be positive: " + ", ".join(invalid))
        if not self.test_command.strip():
            raise ValueError("test_command cannot be blank")
        if self.max_repair_iterations < 0:
            raise ValueError("max_repair_iterations cannot be negative")
        if self.finalization_token_reserve < 0:
            raise ValueError("finalization_token_reserve cannot be negative")
        if self.repair_token_reserve < 0:
            raise ValueError("repair_token_reserve cannot be negative")
        if self.evaluation_token_reserve < 0:
            raise ValueError("evaluation_token_reserve cannot be negative")
        if self.max_post_test_iterations < 0:
            raise ValueError("max_post_test_iterations cannot be negative")
        if self.max_no_mutation_iterations < 0:
            raise ValueError("max_no_mutation_iterations cannot be negative")
        if self.execution_platform and not self.execution_image:
            raise ValueError("execution_platform requires execution_image")
        if self.execution_setup_command and not self.execution_image:
            raise ValueError("execution_setup_command requires execution_image")
        if self.max_cost is not None and self.max_cost <= 0:
            raise ValueError("max_cost must be positive when configured")
