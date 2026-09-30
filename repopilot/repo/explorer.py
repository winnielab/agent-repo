"""Read-only repository analysis using the shared AgentRuntime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from repopilot.agents.explorer import ExplorerAgent
from repopilot.core.context import ContextManager
from repopilot.core.models import AgentTask, Serializable
from repopilot.core.runtime import AgentRuntime
from repopilot.core.structured import parse_json_object
from repopilot.execution.paths import safe_workspace_path
from repopilot.tools.base import ToolContext
from repopilot.task.specification import TaskSpecification


@dataclass(frozen=True)
class RepositoryObservation(Serializable):
    """One source-backed relation in the task-relevant repository slice."""

    key: str
    kind: str
    path: str
    line: int
    symbol: str = ""
    detail: str = ""

    KINDS = frozenset({"instruction", "definition", "caller", "state", "test", "dependency"})

    def validate(self) -> None:
        if not self.key.strip() or not self.path.strip() or self.line < 1:
            raise ValueError("repository observations require key, path, and positive line")
        if self.kind not in self.KINDS:
            raise ValueError("repository observation has an unsupported kind")


@dataclass(frozen=True)
class RepositoryContext(Serializable):
    language: str
    frameworks: List[str] = field(default_factory=list)
    test_command: str = ""
    important_files: List[str] = field(default_factory=list)
    structure_summary: str = ""
    build_system: str = "unknown"
    target_symbols: List[str] = field(default_factory=list)
    implementation_files: List[str] = field(default_factory=list)
    test_files: List[str] = field(default_factory=list)
    instruction_files: List[str] = field(default_factory=list)
    observations: List[RepositoryObservation] = field(default_factory=list)


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
        test_files = [name for name in important if "test" in Path(name).name.casefold()]
        implementation_files = [name for name in important if name not in test_files]
        instructions = [name for name in files if Path(name).name in {"AGENTS.md", "CLAUDE.md"}]
        return RepositoryContext(
            language, [], configured_test, important,
            f"{len(files)} files; top-level: {', '.join(files[:20])}", build,
            [], implementation_files, test_files, instructions, [],
        )

    async def inspect(self, configured_test: str,
                      specification: Optional[TaskSpecification] = None) -> RepositoryContext:
        agent = ExplorerAgent()
        task_contract = specification.to_dict() if specification is not None else {
            "title": "Repository inspection",
            "description": "Identify the implementation and test structure needed by the configured task.",
        }
        task = AgentTask(
            "Inspect only the repository slice needed for this task. Start from issue terms and symbols, "
            "then follow the smallest useful chain through definitions, callers or state readers/writers, "
            "and adjacent tests. Discover AGENTS.md or CLAUDE.md instructions when present. Use read-only "
            "tools as needed and return only source-backed observations with real paths and line numbers. "
            "Do not build a full repository map.\n"
            f"Task contract: {task_contract}\n"
            f"The user-configured test command is: {configured_test}"
        )
        context = ContextManager(agent.system_prompt, task, self.artifact_dir,
                                 self.runtime.event_bus, self.context_char_budget)
        result = await self.runtime.run(agent, task, context, self.tool_context)
        try:
            if not result.success:
                raise ValueError(result.error or "explorer failed")
            data = parse_json_object(result.final_text)
            observations = [RepositoryObservation(
                key=str(item["key"]).strip(),
                kind=str(item["kind"]).strip(),
                path=str(item["path"]).strip(),
                line=int(item["line"]),
                symbol=str(item.get("symbol", "")).strip(),
                detail=str(item.get("detail", "")).strip(),
            ) for item in data.get("observations", [])]
            if len(observations) > 16:
                raise ValueError("repository slice contains too many observations")
            for observation in observations:
                observation.validate()
                evidence_path = safe_workspace_path(
                    self.tool_context.workspace, observation.path, must_exist=True,
                )
                if not evidence_path.is_file():
                    raise ValueError("repository observation path is not a file")
                line_count = len(evidence_path.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines())
                if observation.line > max(1, line_count):
                    raise ValueError("repository observation line is outside the observed file")
            observation_keys = [item.key for item in observations]
            if len(observation_keys) != len(set(observation_keys)):
                raise ValueError("repository observation keys must be unique")

            observed_files = set(self.tool_context.values.get("read_files", []))
            observed_files.update(self.tool_context.values.get("searched_files", []))
            unobserved = [item.path for item in observations if item.path not in observed_files]
            if unobserved:
                raise ValueError(
                    "repository observations cite files not inspected by tools: "
                    + ", ".join(sorted(set(unobserved))[:10])
                )

            implementation_files = [str(item) for item in data.get("implementation_files", [])]
            test_files = [str(item) for item in data.get("test_files", [])]
            instruction_files = [str(item) for item in data.get("instruction_files", [])]
            important_files = list(dict.fromkeys([
                *implementation_files,
                *test_files,
                *instruction_files,
                *[str(item) for item in data.get("important_files", [])],
                *[item.path for item in observations],
            ]))[:40]
            repository = RepositoryContext(
                language=str(data["language"]),
                frameworks=[str(item) for item in data.get("frameworks", [])],
                test_command=str(data.get("test_command") or configured_test),
                important_files=important_files,
                structure_summary=str(data["structure_summary"]),
                build_system=str(data.get("build_system", "unknown")),
                target_symbols=[str(item) for item in data.get("target_symbols", [])][:20],
                implementation_files=implementation_files[:20],
                test_files=test_files[:20],
                instruction_files=instruction_files[:20],
                observations=observations,
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
