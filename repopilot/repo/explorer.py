"""Read-only repository analysis using the shared AgentRuntime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from repopilot.agents.explorer import ExplorerAgent
from repopilot.core.context import ContextManager
from repopilot.core.models import AgentTask, Serializable
from repopilot.core.runtime import AgentRuntime
from repopilot.core.structured import parse_json_object
from repopilot.tools.base import ToolContext


@dataclass(frozen=True)
class RepositoryContext(Serializable):
    language: str
    frameworks: List[str] = field(default_factory=list)
    test_command: str = ""
    important_files: List[str] = field(default_factory=list)
    structure_summary: str = ""
    build_system: str = "unknown"


class RepositoryExplorer:
    def __init__(self, runtime: AgentRuntime, tool_context: ToolContext, artifact_dir: Path,
                 context_char_budget: int = 60_000) -> None:
        self.runtime = runtime
        self.tool_context = tool_context
        self.artifact_dir = artifact_dir
        self.context_char_budget = context_char_budget

    @staticmethod
    def local_fallback(workspace: Path, configured_test: str) -> RepositoryContext:
        files = [path.relative_to(workspace).as_posix() for path in sorted(workspace.rglob("*"))
                 if path.is_file() and ".git" not in path.parts][:80]
        suffixes = {Path(name).suffix for name in files}
        if ".py" in suffixes:
            language = "python"
        elif suffixes & {".ts", ".tsx", ".js", ".jsx"}:
            language = "javascript/typescript"
        elif ".go" in suffixes:
            language = "go"
        else:
            language = "unknown"
        build = next((name for name in ("pyproject.toml", "package.json", "go.mod", "Cargo.toml") if name in files), "unknown")
        manifest_names = {"pyproject.toml", "package.json", "go.mod", "Cargo.toml", "README.md"}
        source_suffixes = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb"}
        # A provider failure must not leave planning with test files but no
        # implementation contract. Preserve repository order while selecting
        # both manifests and representative source/test files.
        important = list(dict.fromkeys(
            [name for name in files if Path(name).name in manifest_names]
            + [name for name in files if Path(name).suffix in source_suffixes]
        ))[:20]
        return RepositoryContext(language, [], configured_test, important,
                                 f"{len(files)} files; top-level: {', '.join(files[:20])}", build)

    async def inspect(self, configured_test: str) -> RepositoryContext:
        agent = ExplorerAgent()
        task = AgentTask(
            "Inspect this repository. Use read-only tools as needed, then return the required JSON object. "
            f"The user-configured test command is: {configured_test}"
        )
        context = ContextManager(agent.system_prompt, task, self.artifact_dir,
                                 self.runtime.event_bus, self.context_char_budget)
        result = await self.runtime.run(agent, task, context, self.tool_context)
        try:
            if not result.success:
                raise ValueError(result.error or "explorer failed")
            data = parse_json_object(result.final_text)
            repository = RepositoryContext(
                language=str(data["language"]),
                frameworks=[str(item) for item in data.get("frameworks", [])],
                test_command=str(data.get("test_command") or configured_test),
                important_files=[str(item) for item in data.get("important_files", [])],
                structure_summary=str(data["structure_summary"]),
                build_system=str(data.get("build_system", "unknown")),
            )
            if not repository.language or not repository.structure_summary:
                raise ValueError("incomplete repository context")
            self.runtime.event_bus.emit("repository_explored", fallback=False,
                                        language=repository.language,
                                        important_files=repository.important_files)
            return repository
        except Exception as exc:
            fallback = self.local_fallback(self.tool_context.workspace, configured_test)
            self.runtime.event_bus.emit("repository_explored", fallback=True,
                                        language=fallback.language, error=f"{type(exc).__name__}: {exc}")
            return fallback
