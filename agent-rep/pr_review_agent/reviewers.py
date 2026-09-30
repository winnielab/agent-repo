"""Modularized reviewers component of the PR review agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .domain import Finding, FindingCollector, Transcript
from .settings import PRIVATE_KEY_RE, SECRET_RE
from .tools import ReviewTools
from .utils import is_source_file, is_test_file, iter_added_lines, truncate


class SpecialtyReviewers:
    def __init__(self, tools: ReviewTools, collector: FindingCollector, transcript: Transcript):
        self.tools = tools
        self.collector = collector
        self.transcript = transcript

    def run(self, name: str, diff: str, files: list[dict[str, Any]], test_result: dict[str, Any] | None) -> str:
        self.transcript.emit("subagent.spawn", name=name)
        if name == "security-reviewer":
            count = self.security(diff)
        elif name == "correctness-reviewer":
            count = self.correctness(diff)
        elif name == "test-reviewer":
            count = self.testing(files, test_result)
        elif name == "maintainability-reviewer":
            count = self.maintainability(diff, files)
        elif name == "company-policy-reviewer":
            count = self.company_policy()
        else:
            raise ValueError(f"Unknown reviewer: {name}")
        self.transcript.emit("subagent.complete", name=name, findings=count)
        return f"{name} completed with {count} finding(s)"

    def security(self, diff: str) -> int:
        before = len(self.collector.findings)
        for added in iter_added_lines(diff):
            text = added["text"].strip()
            secret = PRIVATE_KEY_RE.search(text) or SECRET_RE.search(text)
            if secret:
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P1",
                        category="security",
                        title="Likely secret committed in code",
                        evidence=text[:200],
                        impact="Credentials in source control can be copied from every clone and CI log.",
                        suggestion="Move the value to a secret manager or environment variable and rotate the leaked credential.",
                    )
                )
            if re.search(r"\bos\.system\(|subprocess\.[^(]+\(.*shell\s*=\s*True", text):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P1",
                        category="security",
                        title="Shell command is built on an unsafe execution surface",
                        evidence=text[:200],
                        impact="User-controlled input can become command execution if it reaches this call.",
                        suggestion="Use subprocess with an argument list, validate inputs, and keep shell=False.",
                    )
                )
            if re.search(r"\.execute\(f[\"']", text) or re.search(r"\.execute\(.*\+.*\)", text):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P1",
                        category="security",
                        title="SQL query appears to interpolate values directly",
                        evidence=text[:200],
                        impact="Interpolated SQL can allow injection and data exposure.",
                        suggestion="Use parameterized queries provided by the database driver.",
                    )
                )
        return len(self.collector.findings) - before

    def maintainability(self, diff: str, files: list[dict[str, Any]]) -> int:
        before = len(self.collector.findings)
        for item in files:
            added = item.get("added") or 0
            deleted = item.get("deleted") or 0
            path = item.get("file", "")
            if is_source_file(path) and added + deleted >= 250:
                self.collector.emit(
                    Finding(
                        file=path,
                        line=1,
                        severity="P3",
                        category="maintainability",
                        title="Large source change should be split or explained",
                        evidence=f"{added} added lines, {deleted} deleted lines",
                        impact="Large mixed changes are harder to review and increase regression risk.",
                        suggestion="Split unrelated changes or add a short design note explaining the review strategy.",
                    )
                )
        for added in iter_added_lines(diff):
            stripped = added["text"].strip()
            if "TODO" in stripped or "FIXME" in stripped or "HACK" in stripped:
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P3",
                        category="maintainability",
                        title="New unresolved marker added to production code",
                        evidence=stripped[:200],
                        impact="Markers like TODO/FIXME can hide incomplete behavior after merge.",
                        suggestion="Resolve the marker before merge or link it to a tracked follow-up task.",
                    )
                )
        return len(self.collector.findings) - before

    def correctness(self, diff: str) -> int:
        before = len(self.collector.findings)
        previous_except: dict[str, int] = {}
        for added in iter_added_lines(diff):
            text = added["text"].rstrip()
            stripped = text.strip()
            if re.match(r"def .*=\s*(\[\]|\{\})", stripped):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P2",
                        category="correctness",
                        title="Mutable default argument can leak state between calls",
                        evidence=stripped[:200],
                        impact="The same list or dict instance is reused across calls, causing surprising cross-request state.",
                        suggestion="Default to None and create the mutable object inside the function.",
                    )
                )
            if re.match(r"except(?:\s+Exception)?\s*:$", stripped):
                previous_except[added["file"]] = added["line"]
            if re.match(r"except(?:\s+Exception)?\s*:\s*pass$", stripped) or (
                stripped == "pass" and previous_except.get(added["file"]) == added["line"] - 1
            ):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=previous_except.get(added["file"], added["line"]),
                        severity="P2",
                        category="correctness",
                        title="Exception is swallowed without handling",
                        evidence=stripped,
                        impact="Failures disappear, making data loss and partial writes hard to detect.",
                        suggestion="Handle the specific exception, log enough context, or re-raise after cleanup.",
                    )
                )
            if " == None" in stripped or " != None" in stripped:
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P3",
                        category="maintainability",
                        title="None comparison should use identity checks",
                        evidence=stripped,
                        impact="Equality operators can be overloaded and make null checks less predictable.",
                        suggestion="Use 'is None' or 'is not None'.",
                    )
                )
        return len(self.collector.findings) - before

    def testing(self, files: list[dict[str, Any]], test_result: dict[str, Any] | None) -> int:
        before = len(self.collector.findings)
        changed = [f["file"] for f in files]
        source_changed = [f for f in changed if is_source_file(f) and not is_test_file(f)]
        tests_changed = [f for f in changed if is_test_file(f)]
        if source_changed and not tests_changed:
            first = source_changed[0]
            self.collector.emit(
                Finding(
                    file=first,
                    line=1,
                    severity="P2",
                    category="testing",
                    title="Production code changed without nearby test changes",
                    evidence=", ".join(source_changed[:5]),
                    impact="The PR can regress behavior without an automated signal catching it.",
                    suggestion="Add or update tests that cover the changed behavior, especially edge cases and failure paths.",
                )
            )
        if test_result and test_result.get("returncode", 0) != 0:
            self.collector.emit(
                Finding(
                    file=source_changed[0] if source_changed else ".",
                    line=1,
                    severity="P1",
                    category="testing",
                    title="Configured test command failed",
                    evidence=truncate(str(test_result.get("output", "")), 1000),
                    impact="A failing test suite means the branch is not safe to merge.",
                    suggestion="Fix the failing tests or update the implementation if the failures expose a regression.",
                )
            )
        return len(self.collector.findings) - before

    def company_policy(self) -> int:
        before = len(self.collector.findings)
        policy_map = {
            "sql_interpolation": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy requires parameterized SQL",
                "impact": "This violates the company SQL baseline and can expose payment data through injection.",
                "suggestion": "Use database-driver parameter binding and add a malicious identifier regression test.",
            },
            "shell_execution": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy blocks shell execution with request input",
                "impact": "This violates the command execution baseline for request paths and can become command injection.",
                "suggestion": "Use an argument list with shell=False and validate every argument.",
            },
            "sensitive_logging": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy forbids logging payment secrets",
                "impact": "This violates sensitive logging rules and can leak credentials or card data into log systems.",
                "suggestion": "Remove the sensitive fields or use masked logging helpers.",
            },
            "signature_mismatch_not_rejected": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy requires webhook signature failures to reject",
                "impact": "This violates payment webhook policy and can accept forged or replayed payment callbacks.",
                "suggestion": "Reject invalid signatures before any state mutation and use constant-time comparison.",
            },
            "swallowed_exception": {
                "severity": "P2",
                "category": "correctness",
                "title": "Company policy requires risk controls to fail closed",
                "impact": "This violates payment risk policy because failures can disappear and default to unsafe behavior.",
                "suggestion": "Handle specific exceptions and return a conservative manual review decision.",
            },
            "no_tests_changed": {
                "severity": "P2",
                "category": "testing",
                "title": "Company policy requires tests for payment-sensitive changes",
                "impact": "This violates testing policy for payment, security, or risk-control changes.",
                "suggestion": "Add targeted tests for the changed payment behavior and failure paths.",
            },
        }
        for signal in self.tools.risk_scan():
            spec = policy_map.get(signal.get("signal"))
            if not spec:
                continue
            policies = self.tools.retrieve_company_policy(
                " ".join([str(signal.get("signal", "")), str(signal.get("evidence", "")), str(signal.get("rationale", ""))]),
                category=str(spec["category"]),
                max_results=2,
            )
            if not policies:
                continue
            policy_text = "; ".join(policy["policy_id"] for policy in policies)
            self.collector.emit(
                Finding(
                    file=str(signal["file"]),
                    line=int(signal["line"]),
                    severity=str(spec["severity"]),
                    category=str(spec["category"]),
                    title=str(spec["title"]),
                    evidence=f"{signal['evidence']} | company policy: {policy_text}",
                    impact=str(spec["impact"]),
                    suggestion=str(spec["suggestion"]),
                )
            )
        return len(self.collector.findings) - before
