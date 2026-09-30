"""ExecutionEnvironment protocol and bounded local implementation."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from pathlib import PurePosixPath
from typing import Dict, Mapping, Optional, Protocol

from repopilot.core.errors import CommandExecutionError
from repopilot.core.models import CommandResult
from repopilot.execution.paths import safe_cwd


class ExecutionEnvironment(Protocol):
    workspace: Path

    async def execute(
        self,
        command: str,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        ...


class LocalEnvironment:
    DEFAULT_ENV_ALLOWLIST = (
        "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "HOME", "USER",
        "PYTHONPATH", "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
    )

    def __init__(
        self,
        workspace: Path,
        default_timeout: float = 30.0,
        max_output_chars: int = 50_000,
        base_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars
        source = base_env or os.environ
        self.base_env: Dict[str, str] = {
            key: source[key] for key in self.DEFAULT_ENV_ALLOWLIST if key in source
        }
        self.base_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        self.base_env.setdefault("PYTHONHASHSEED", "0")

    def _bounded(self, text: str) -> tuple[str, bool]:
        if len(text) <= self.max_output_chars:
            return text, False
        removed = len(text) - self.max_output_chars
        marker = f"\n... truncated {removed} chars ...\n"
        if len(marker) >= self.max_output_chars:
            marker = "\n<truncated>\n"
        available = max(0, self.max_output_chars - len(marker))
        head_chars = available // 2
        tail_chars = available - head_chars
        tail = text[-tail_chars:] if tail_chars else ""
        return text[:head_chars] + marker + tail, True

    async def execute(
        self,
        command: str,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        working_dir = safe_cwd(self.workspace, cwd or self.workspace)
        child_env = dict(self.base_env)
        if env:
            child_env.update({str(key): str(value) for key, value in env.items()})
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(working_dir),
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise CommandExecutionError(f"could not start command: {exc}") from exc
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self.default_timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, 9)
            except (ProcessLookupError, PermissionError):
                process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
        stdout, out_truncated = self._bounded(stdout_bytes.decode("utf-8", errors="replace"))
        stderr, err_truncated = self._bounded(stderr_bytes.decode("utf-8", errors="replace"))
        return CommandResult(
            command=command,
            returncode=124 if timed_out else int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            output_truncated=out_truncated or err_truncated,
        )


class DockerEnvironment(LocalEnvironment):
    """Run model-requested shell commands in a task image with only the workspace mounted."""

    def __init__(
        self,
        workspace: Path,
        image: str,
        platform: Optional[str] = None,
        setup_command: str = "",
        default_timeout: float = 30.0,
        max_output_chars: int = 50_000,
        base_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(workspace, default_timeout, max_output_chars, base_env)
        if not image.strip():
            raise ValueError("Docker execution image cannot be blank")
        self.image = image.strip()
        self.platform = platform.strip() if platform else None
        self.setup_command = setup_command.strip()
        self.git_common_dir = self._git_common_dir()

    def _git_common_dir(self) -> Optional[Path]:
        marker = self.workspace / ".git"
        if not marker.is_file():
            return None
        first_line = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if not first_line.startswith("gitdir: "):
            return None
        git_dir = Path(first_line.removeprefix("gitdir: ").strip())
        if not git_dir.is_absolute():
            git_dir = (self.workspace / git_dir).resolve()
        common_marker = git_dir / "commondir"
        if not common_marker.is_file():
            return git_dir
        common = Path(common_marker.read_text(encoding="utf-8").strip())
        return common if common.is_absolute() else (git_dir / common).resolve()

    def command_args(self, command: str, cwd: Optional[Path] = None) -> list[str]:
        working_dir = safe_cwd(self.workspace, cwd or self.workspace)
        relative = working_dir.relative_to(self.workspace)
        container_cwd = str(PurePosixPath("/workspace", *relative.parts))
        wrapped = f"{self.setup_command} && {command}" if self.setup_command else command
        args = ["docker", "run", "--rm"]
        if self.platform:
            args.extend(["--platform", self.platform])
        args.extend([
            "--mount", f"type=bind,src={self.workspace},dst=/workspace",
            "-w", container_cwd,
        ])
        if self.git_common_dir:
            args.extend([
                "--mount",
                f"type=bind,src={self.git_common_dir},dst={self.git_common_dir}",
            ])
        for key in ("LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED"):
            if key in self.base_env:
                args.extend(["-e", f"{key}={self.base_env[key]}"])
        args.extend([self.image, "bash", "-lc", wrapped])
        return args

    async def execute(
        self,
        command: str,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        if env:
            raise ValueError("per-command environment overrides are not supported in Docker execution")
        args = self.command_args(command, cwd)
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise CommandExecutionError(f"could not start Docker command: {exc}") from exc
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self.default_timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, 9)
            except (ProcessLookupError, PermissionError):
                process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
        stdout, out_truncated = self._bounded(stdout_bytes.decode("utf-8", errors="replace"))
        stderr, err_truncated = self._bounded(stderr_bytes.decode("utf-8", errors="replace"))
        return CommandResult(
            command=command,
            returncode=124 if timed_out else int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            output_truncated=out_truncated or err_truncated,
        )
