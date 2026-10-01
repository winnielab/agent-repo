"""Configured test execution; the model cannot replace the command."""

import re
from typing import Any, Dict, List, Optional

from repopilot.core.models import CommandResult, ToolResult
from repopilot.execution.environment import ExecutionEnvironment
from repopilot.tools.base import ToolContext, function_schema


class RunTestsTool:
    name = "run_tests"
    validates_workspace = True
    description = "Run the test command configured by the user for this solve run."

    def __init__(self, command: str, timeout: float = 60.0,
                 environment: Optional[ExecutionEnvironment] = None) -> None:
        self.command = command
        self.timeout = timeout
        self.configured_timeout = timeout
        self.environment = environment
        self.last_result: Optional[CommandResult] = None
        self.baseline_result: Optional[CommandResult] = None

    @staticmethod
    def failure_ids(result: CommandResult) -> set[str]:
        """Extract stable pytest/unittest failure identifiers from noisy output."""
        # A successful process is authoritative.  Test suites can legitimately
        # print strings such as ``FAILED test_example.py::test_case`` while
        # testing their own reporting code; treating those strings as real
        # failures creates contradictory evidence for the GoalEvaluator.
        if result.ok:
            return set()
        identifiers: set[str] = set()
        output = result.stdout + "\n" + result.stderr
        # Pytest's final summary is a stronger observation than arbitrary
        # captured output from tests that exercise pytest's own reporter.
        if "short test summary info" in output:
            output = output.rsplit("short test summary info", 1)[-1]
        for raw_line in output.splitlines():
            line = raw_line.strip()
            match = re.match(r"^(?:FAILED|ERROR)\s+([^\s]+(?:::[^\s]+)+)", line)
            if match:
                identifiers.add(match.group(1))
                continue
            match = re.match(r"^(?:FAIL|ERROR):\s+(.+)$", line)
            if match:
                identifiers.add(match.group(1).strip())
        return identifiers

    @staticmethod
    def compatibility_errors(result: CommandResult) -> List[Dict[str, str]]:
        """Return structured target-version symbol errors from a failed run."""
        if result.ok:
            return []
        output = result.stdout + "\n" + result.stderr
        findings: List[Dict[str, str]] = []
        seen = set()
        patterns = (
            (
                "missing_attribute",
                re.compile(
                    r"AttributeError:\s+module\s+['\"](?P<module>[^'\"]+)['\"]\s+"
                    r"has no attribute\s+['\"](?P<symbol>[^'\"]+)['\"]"
                ),
            ),
            (
                "missing_import",
                re.compile(
                    r"ImportError:\s+cannot import name\s+['\"](?P<symbol>[^'\"]+)['\"]\s+"
                    r"from\s+['\"](?P<module>[^'\"]+)['\"]"
                ),
            ),
        )
        for kind, pattern in patterns:
            for match in pattern.finditer(output):
                key = (kind, match.group("module"), match.group("symbol"))
                if key in seen:
                    continue
                seen.add(key)
                findings.append({
                    "kind": kind,
                    "module": match.group("module"),
                    "symbol": match.group("symbol"),
                })
        return findings

    async def establish_baseline(self, context: ToolContext) -> CommandResult:
        context.event_bus.emit("baseline_test_started", command=self.command)
        environment = self.environment or context.environment
        result = await environment.execute(self.command, timeout=self.timeout)
        self.baseline_result = result
        context.event_bus.emit(
            "baseline_test_finished", command=self.command, ok=result.ok,
            returncode=result.returncode, duration_seconds=result.duration_seconds,
            failure_ids=sorted(self.failure_ids(result)),
            compatibility_errors=self.compatibility_errors(result),
        )
        return result

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object", "properties": {}, "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext, *,
                      timeout: Optional[float] = None,
                      timeout_reason: str = "configured") -> ToolResult:
        if arguments:
            return ToolResult(False, "run_tests does not accept command overrides", error_code="invalid_arguments")
        effective_timeout = self.configured_timeout if timeout is None else timeout
        context.event_bus.emit(
            "test_started",
            command=self.command,
            configured_timeout_seconds=self.configured_timeout,
            effective_timeout_seconds=effective_timeout,
            timeout_reason=timeout_reason,
        )
        environment = self.environment or context.environment
        result = await environment.execute(self.command, timeout=effective_timeout)
        self.last_result = result
        raw_ok = result.ok
        baseline_failures = self.failure_ids(self.baseline_result) if self.baseline_result else set()
        current_failures = self.failure_ids(result)
        baseline_compatibility = (
            self.compatibility_errors(self.baseline_result) if self.baseline_result else []
        )
        current_compatibility = self.compatibility_errors(result)
        baseline_compatibility_keys = {
            (item["kind"], item["module"], item["symbol"])
            for item in baseline_compatibility
        }
        compatibility_errors = [
            item for item in current_compatibility
            if (item["kind"], item["module"], item["symbol"])
            not in baseline_compatibility_keys
        ]
        baseline_matched = bool(
            self.baseline_result
            and not self.baseline_result.ok
            and baseline_failures
            and current_failures
            and not (current_failures - baseline_failures)
        )
        effective_ok = raw_ok or baseline_matched
        context.event_bus.emit(
            "test_finished",
            command=self.command,
            ok=effective_ok,
            raw_ok=raw_ok,
            baseline_matched=baseline_matched,
            returncode=result.returncode,
            duration_seconds=result.duration_seconds,
            configured_timeout_seconds=self.configured_timeout,
            effective_timeout_seconds=effective_timeout,
            timeout_reason=timeout_reason,
            new_failure_ids=sorted(current_failures - baseline_failures),
            compatibility_errors=compatibility_errors,
        )
        output = (result.stdout + result.stderr).strip() or "(no output)"
        if baseline_matched and not raw_ok:
            output += (
                "\n\nConfigured tests still contain only recognized failures that existed "
                "before editing; no new regression failure identifiers were introduced."
            )
        if compatibility_errors:
            symbols = ", ".join(
                f"{item['module']}.{item['symbol']}" for item in compatibility_errors
            )
            output += (
                "\n\nTarget-version compatibility guard: the configured environment reports "
                f"missing symbol(s): {symbols}. Do not retry the same speculative API. "
                "Search the current checkout for an existing definition or neighboring usage, "
                "replace the unavailable symbol with a version-compatible local abstraction, "
                "and rerun the configured tests."
            )
        if result.timed_out and timeout_reason != "configured":
            output += (
                "\n\nWorkflow deadline guard: this test received "
                f"{effective_timeout:.3f}s of the configured {self.configured_timeout:.3f}s "
                f"timeout because {timeout_reason}. This is a workflow-budget timeout, not "
                "evidence that the candidate assertion behavior is wrong."
            )
        if effective_ok:
            output += ("\n\nConfigured tests passed. Inspect git_diff, remove temporary files, "
                       "and finish. Do not run additional ad-hoc verification unless the "
                       "diff reveals a concrete unresolved problem.")
        metadata = result.to_dict()
        metadata.update({
            "raw_ok": raw_ok,
            "baseline_aware": self.baseline_result is not None,
            "baseline_matched": baseline_matched,
            "baseline_failure_ids": sorted(baseline_failures),
            "current_failure_ids": sorted(current_failures),
            "new_failure_ids": sorted(current_failures - baseline_failures),
            "baseline_compatibility_errors": baseline_compatibility,
            "current_compatibility_errors": current_compatibility,
            "compatibility_errors": compatibility_errors,
            "configured_timeout_seconds": self.configured_timeout,
            "effective_timeout_seconds": effective_timeout,
            "timeout_reason": timeout_reason,
        })
        return ToolResult(effective_ok, output, metadata,
                          None if effective_ok else "tests_failed")
