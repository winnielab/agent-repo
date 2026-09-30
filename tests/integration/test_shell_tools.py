import asyncio
import sys

from repopilot.core.models import ToolCall
from repopilot.execution.environment import LocalEnvironment
from repopilot.execution.permission import PermissionDecision, PermissionManager
from repopilot.observability.events import EventBus
from repopilot.tools.base import ToolContext
from repopilot.tools.registry import ToolRegistry
from repopilot.tools.shell import BashTool
from repopilot.tools.testing import RunTestsTool


def test_shell_and_configured_tests(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path)
    context = ToolContext(tmp_path, environment, "run", EventBus("run"))
    registry = ToolRegistry([BashTool(), RunTestsTool(f'{sys.executable} -c "print(\'ok\')"')])
    shell = asyncio.run(registry.execute(ToolCall("1", "bash", {"command": "pwd"}), context))
    tests = asyncio.run(registry.execute(ToolCall("2", "run_tests", {}), context))
    assert shell.ok and str(tmp_path) in shell.content
    assert context.values["command_evidence"][-1]["command"] == "pwd"
    assert context.values["command_evidence"][-1]["ok"] is True
    assert tests.ok and "ok" in tests.content

    failing_registry = ToolRegistry([RunTestsTool(f'{sys.executable} -c "import sys;print(\'bad\');sys.exit(3)"')])
    failed = asyncio.run(failing_registry.execute(ToolCall("3", "run_tests", {}), context))
    assert not failed.ok
    assert failed.metadata["returncode"] == 3
    assert "bad" in failed.metadata["stdout"]


def test_dangerous_shell_never_reaches_environment(tmp_path) -> None:
    manager = PermissionManager(tmp_path, allowed_command_prefixes=("git",))
    decision, _ = manager.check(ToolCall("1", "bash", {"command": "git reset --hard"}))
    assert decision is PermissionDecision.DENY


def test_configured_tests_can_use_a_separate_host_environment(tmp_path) -> None:
    agent_workspace = tmp_path / "agent"
    test_workspace = tmp_path / "tests"
    agent_workspace.mkdir()
    test_workspace.mkdir()
    context = ToolContext(
        agent_workspace, LocalEnvironment(agent_workspace), "run", EventBus("run"),
    )
    tool = RunTestsTool("pwd", environment=LocalEnvironment(test_workspace))
    result = asyncio.run(tool.execute({}, context))
    assert result.ok
    assert str(test_workspace) in result.content
    assert str(agent_workspace) not in result.metadata["stdout"]


def test_configured_tests_accept_only_recognized_preexisting_failures(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path)
    context = ToolContext(tmp_path, environment, "run", EventBus("run"))
    old_failure = "FAILED tests/test_old.py::test_old - AssertionError"
    command = f'{sys.executable} -c "import sys; print(\'{old_failure}\'); sys.exit(1)"'
    tool = RunTestsTool(command, environment=environment)
    asyncio.run(tool.establish_baseline(context))
    unchanged = asyncio.run(tool.execute({}, context))
    assert unchanged.ok
    assert unchanged.metadata["baseline_matched"] is True

    new_failure = "FAILED tests/test_new.py::test_new - AssertionError"
    tool.command = (
        f'{sys.executable} -c "import sys; print(\'{old_failure}\'); '
        f'print(\'{new_failure}\'); sys.exit(1)"'
    )
    regressed = asyncio.run(tool.execute({}, context))
    assert not regressed.ok
    assert regressed.metadata["new_failure_ids"] == ["tests/test_new.py::test_new"]

    tool.command = f'{sys.executable} -c "import sys; print(\'compiler crashed\'); sys.exit(1)"'
    unrecognized_failure = asyncio.run(tool.execute({}, context))
    assert not unrecognized_failure.ok
    assert unrecognized_failure.metadata["baseline_matched"] is False


def test_passing_command_cannot_report_failure_ids_from_embedded_output(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path)
    context = ToolContext(tmp_path, environment, "run", EventBus("run"))
    embedded = "FAILED test_report.py::Example::test_expected - AssertionError"
    tool = RunTestsTool(
        f'{sys.executable} -c "print(\'{embedded}\'); print(\'59 passed, 29 skipped\')"',
        environment=environment,
    )

    result = asyncio.run(tool.execute({}, context))

    assert result.ok
    assert result.metadata["raw_ok"] is True
    assert result.metadata["current_failure_ids"] == []
    assert result.metadata["new_failure_ids"] == []


def test_missing_dependency_symbol_is_structured_repair_evidence(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path)
    context = ToolContext(tmp_path, environment, "run", EventBus("run"))
    message = "AttributeError: module 'framework.nodes' has no attribute 'NewNode'"
    tool = RunTestsTool(
        f'{sys.executable} -c "import sys; print(\'{message}\'); sys.exit(1)"',
        environment=environment,
    )

    result = asyncio.run(tool.execute({}, context))

    assert not result.ok
    assert result.metadata["compatibility_errors"] == [{
        "kind": "missing_attribute",
        "module": "framework.nodes",
        "symbol": "NewNode",
    }]
    assert "Target-version compatibility guard" in result.content
    assert "framework.nodes.NewNode" in result.content

    asyncio.run(tool.establish_baseline(context))
    unchanged = asyncio.run(tool.execute({}, context))
    assert unchanged.metadata["current_compatibility_errors"]
    assert unchanged.metadata["compatibility_errors"] == []
    assert "Target-version compatibility guard" not in unchanged.content


def test_pytest_failure_ids_prefer_final_summary_over_captured_examples(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path)
    context = ToolContext(tmp_path, environment, "run", EventBus("run"))
    command = (
        f'{sys.executable} -c "import sys; '
        "print('FAILED tests/test_report.py::test_embedded - expected text'); "
        "print('================ short test summary info ================'); "
        "print('FAILED tests/test_real.py::test_real - AssertionError'); "
        'sys.exit(1)"'
    )
    tool = RunTestsTool(command, environment=environment)

    result = asyncio.run(tool.execute({}, context))

    assert not result.ok
    assert result.metadata["current_failure_ids"] == ["tests/test_real.py::test_real"]


def test_test_timeout_reports_configured_and_workflow_deadline_values(tmp_path) -> None:
    environment = LocalEnvironment(tmp_path)
    context = ToolContext(tmp_path, environment, "run", EventBus("run"))
    tool = RunTestsTool(
        f'{sys.executable} -c "import time; time.sleep(0.2)"',
        timeout=1.0,
        environment=environment,
    )

    result = asyncio.run(tool.execute(
        {}, context, timeout=0.01,
        timeout_reason="the issue workflow deadline had less time remaining",
    ))

    assert not result.ok
    assert result.metadata["timed_out"] is True
    assert result.metadata["configured_timeout_seconds"] == 1.0
    assert result.metadata["effective_timeout_seconds"] == 0.01
    assert "Workflow deadline guard" in result.content
