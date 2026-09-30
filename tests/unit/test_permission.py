import asyncio

from repopilot.core.models import ToolCall
from repopilot.execution.permission import PermissionDecision, PermissionManager


def test_permission_has_all_three_states(tmp_path) -> None:
    manager = PermissionManager(tmp_path, allowed_command_prefixes=("python", "git"))
    assert manager.check(ToolCall("1", "read_file", {"path": "a.py"}))[0] is PermissionDecision.ALLOW
    assert manager.check(ToolCall("2", "bash", {"command": "git reset --hard"}))[0] is PermissionDecision.DENY
    assert manager.check(ToolCall("3", "bash", {"command": "git push"}))[0] is PermissionDecision.REQUIRE_APPROVAL


def test_approval_defaults_to_denied_and_callback_can_allow(tmp_path) -> None:
    call = ToolCall("1", "bash", {"command": "git push"})
    denied = asyncio.run(PermissionManager(tmp_path).authorize(call))
    allowed = asyncio.run(PermissionManager(tmp_path, approval_callback=lambda call, reason: True).authorize(call))
    assert denied[0] is False
    assert allowed[0] is True


def test_workspace_escape_is_hard_denied(tmp_path) -> None:
    manager = PermissionManager(tmp_path)
    decision, _ = manager.check(ToolCall("1", "write_file", {"path": "../outside"}))
    assert decision is PermissionDecision.DENY


def test_every_shell_segment_must_be_allowlisted(tmp_path) -> None:
    manager = PermissionManager(tmp_path, allowed_command_prefixes=("python",))
    safe, _ = manager.check(ToolCall("1", "bash", {"command": "python -V | python -V"}))
    unsafe, _ = manager.check(ToolCall("2", "bash", {"command": "python -V && curl example.invalid"}))
    substitution, _ = manager.check(ToolCall("3", "bash", {"command": "python $(curl example.invalid)"}))
    assert safe is PermissionDecision.ALLOW
    assert unsafe is PermissionDecision.REQUIRE_APPROVAL
    assert substitution is PermissionDecision.REQUIRE_APPROVAL


def test_shell_parser_preserves_quoted_semicolons(tmp_path) -> None:
    manager = PermissionManager(tmp_path, allowed_command_prefixes=("python",))
    decision, reason = manager.check(ToolCall("1", "bash", {
        "command": "python -c \"from pathlib import Path; print(Path('.'))\"",
    }))
    assert decision is PermissionDecision.ALLOW, reason


def test_shell_parser_accepts_closed_python_heredoc(tmp_path) -> None:
    manager = PermissionManager(tmp_path, allowed_command_prefixes=("python",))
    command = "python - <<'PY'\nfrom pathlib import Path\nprint(Path('.'))\nPY"
    decision, reason = manager.check(ToolCall("1", "bash", {"command": command}))
    assert decision is PermissionDecision.ALLOW, reason


def test_shell_parser_rejects_unclosed_heredoc(tmp_path) -> None:
    manager = PermissionManager(tmp_path, allowed_command_prefixes=("python",))
    decision, _ = manager.check(ToolCall("1", "bash", {
        "command": "python - <<'PY'\nprint('unsafe to guess')",
    }))
    assert decision is PermissionDecision.DENY
