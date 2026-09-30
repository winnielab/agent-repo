"""Three-state policy evaluated at the central tool boundary."""

from __future__ import annotations

import inspect
import re
import shlex
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple, Union

from repopilot.core.models import ToolCall
from repopilot.execution.paths import safe_workspace_path


class PermissionDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


ApprovalCallback = Callable[[ToolCall, str], Union[bool, Awaitable[bool]]]


def _quoted_shell_segments(command: str) -> list[str]:
    """Split shell commands on unquoted control operators."""
    segments: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            index += 1
            continue
        if char in {";", "|", "&", "\n"}:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            if char in {"|", "&"} and index + 1 < len(command) and command[index + 1] == char:
                index += 1
            index += 1
            continue
        current.append(char)
        index += 1
    if quote or escaped:
        raise ValueError("unclosed shell quote or escape")
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _heredoc_executable(command: str) -> Optional[str]:
    """Return the executable for a single, closed heredoc command."""
    lines = command.splitlines()
    if "<<" not in command:
        return None
    if len(lines) < 3 or "<<" not in lines[0]:
        raise ValueError("invalid or unclosed heredoc")
    header, marker = lines[0].split("<<", 1)
    marker = marker.strip()
    if marker.startswith("-"):
        marker = marker[1:].strip()
    if len(marker) >= 2 and marker[0] == marker[-1] and marker[0] in {"'", '"'}:
        marker = marker[1:-1]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", marker):
        raise ValueError("invalid heredoc delimiter")
    if lines[-1].strip() != marker:
        raise ValueError("unclosed heredoc")
    words = shlex.split(header.strip(), posix=True)
    while words and "=" in words[0] and not words[0].startswith(("/", "./")):
        words.pop(0)
    if not words:
        raise ValueError("heredoc command has no executable")
    return Path(words[0]).name


class PermissionManager:
    SAFE_TOOLS = {
        "read_file", "search_code", "glob", "git_diff", "git_status",
        "write_file", "edit_file", "delete_file", "run_tests",
    }
    PATH_TOOLS = {"read_file", "search_code", "glob", "write_file", "edit_file", "delete_file"}
    HARD_DENY = (
        re.compile(r"(^|[;&|]\s*)sudo(?:\s|$)"),
        re.compile(r"(^|[;&|]\s*)(shutdown|reboot|mkfs)(?:\s|$)"),
        re.compile(r"git\s+reset\s+--hard(?:\s|$)"),
        re.compile(r"(^|\s)>\s*/dev/"),
        re.compile(r"\brm\s+-[^\n;|&]*r[^\n;|&]*f\s+/(?:\s|$)"),
    )
    APPROVAL_PATTERNS = (
        re.compile(r"(^|[;&|]\s*)(rm|rmdir)(?:\s|$)"),
        re.compile(r"\bgit\s+push(?:\s|$)"),
        re.compile(r"(^|\s)(>|>>|tee)\s*/"),
        re.compile(r"\$\(|`"),
    )
    HARD_DENY_EXECUTABLES = {"sudo", "shutdown", "reboot", "mkfs"}

    def __init__(
        self,
        workspace: Path,
        allowed_command_prefixes: Iterable[str] = (),
        approval_callback: Optional[ApprovalCallback] = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.allowed_command_prefixes: Tuple[str, ...] = tuple(allowed_command_prefixes)
        self.approval_callback = approval_callback

    def check(self, call: ToolCall) -> tuple[PermissionDecision, str]:
        if call.name in self.PATH_TOOLS:
            path = call.arguments.get("path", ".")
            if not isinstance(path, str):
                return PermissionDecision.DENY, "path must be a string"
            try:
                safe_workspace_path(self.workspace, path)
            except Exception:
                return PermissionDecision.DENY, "path escapes workspace"
        if call.name in self.SAFE_TOOLS:
            return PermissionDecision.ALLOW, "registered workspace-scoped tool"
        if call.name != "bash":
            return PermissionDecision.REQUIRE_APPROVAL, "unknown or external tool"

        command = call.arguments.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return PermissionDecision.DENY, "bash command must be a non-empty string"
        if any(pattern.search(command) for pattern in self.HARD_DENY):
            return PermissionDecision.DENY, "command matches hard-deny policy"
        if any(pattern.search(command) for pattern in self.APPROVAL_PATTERNS):
            return PermissionDecision.REQUIRE_APPROVAL, "command can be destructive or external"
        executables = []
        try:
            heredoc_executable = _heredoc_executable(command)
            if heredoc_executable:
                executables.append(heredoc_executable)
            for segment in (() if heredoc_executable else _quoted_shell_segments(command)):
                segment = segment.strip(" ()\t")
                if not segment:
                    continue
                words = shlex.split(segment, posix=True)
                while words and "=" in words[0] and not words[0].startswith(("/", "./")):
                    words.pop(0)
                if words:
                    executables.append(Path(words[0]).name)
        except ValueError:
            return PermissionDecision.DENY, "command could not be parsed"
        if not executables:
            return PermissionDecision.DENY, "command has no executable"
        if any(executable in self.HARD_DENY_EXECUTABLES for executable in executables):
            return PermissionDecision.DENY, "command invokes a hard-denied executable"
        if all(executable in self.allowed_command_prefixes for executable in executables):
            return PermissionDecision.ALLOW, "command executable is allowlisted"
        return PermissionDecision.REQUIRE_APPROVAL, "one or more command executables are not allowlisted"

    async def authorize(self, call: ToolCall) -> tuple[bool, PermissionDecision, str]:
        decision, reason = self.check(call)
        if decision is PermissionDecision.ALLOW:
            return True, decision, reason
        if decision is PermissionDecision.DENY or self.approval_callback is None:
            return False, decision, reason
        approved = self.approval_callback(call, reason)
        if inspect.isawaitable(approved):
            approved = await approved
        return bool(approved), decision, reason
