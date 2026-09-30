"""Modularized tools component of the PR review agent."""

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

from .domain import Transcript
from .knowledge import CompanyKnowledgeBase
from .settings import MAX_DIFF_CHARS, PRIVATE_KEY_RE, SECRET_RE
from .utils import (git_range, is_git_repo, is_source_file, is_test_file,
    iter_added_lines, parse_numstat, run_git, safe_repo_path,
    synthetic_changed_files, synthetic_diff, truncate)


class ReviewTools:
    def __init__(
        self,
        repo: Path,
        base: str,
        target: str,
        transcript: Transcript,
        company_kb: CompanyKnowledgeBase | None = None,
        test_timeout: int = 180,
    ):
        self.repo = repo.resolve()
        self.base = base
        self.target = target
        self.transcript = transcript
        self.company_kb = company_kb
        self.test_timeout = max(1, int(test_timeout))
        self._diff_cache: str | None = None
        self._files_cache: list[dict[str, Any]] | None = None

    def git_diff(self, base: str | None = None, target: str | None = None) -> str:
        base = base or self.base
        target = target or self.target
        if not is_git_repo(self.repo):
            diff = synthetic_diff(self.repo)
            self._diff_cache = diff
            self.transcript.emit("tool.git_diff.fallback", reason="not a git repository", chars=len(diff))
            return diff
        revision_range = git_range(self.repo, base, target)
        diff = run_git(self.repo, ["diff", "--find-renames", revision_range], timeout=60)
        diff = truncate(diff, MAX_DIFF_CHARS)
        self._diff_cache = diff
        self.transcript.emit("tool.git_diff", base=base, target=target, chars=len(diff))
        return diff

    def changed_files(self, base: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
        base = base or self.base
        target = target or self.target
        if not is_git_repo(self.repo):
            files = synthetic_changed_files(self.repo)
            self._files_cache = files
            self.transcript.emit("tool.changed_files.fallback", reason="not a git repository", count=len(files))
            return files
        revision_range = git_range(self.repo, base, target)
        output = run_git(self.repo, ["diff", "--numstat", "--find-renames", revision_range], timeout=60)
        files = parse_numstat(output)
        self._files_cache = files
        self.transcript.emit("tool.changed_files", count=len(files), files=files)
        return files

    def read_file_context(self, path: str, line: int, radius: int = 4) -> str:
        fp = safe_repo_path(self.repo, path)
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, line - max(0, radius))
        end = min(len(lines), line + max(0, radius))
        rendered = []
        for number in range(start, end + 1):
            rendered.append(f"{number:>5}: {lines[number - 1]}")
        result = "\n".join(rendered)
        self.transcript.emit("tool.read_file_context", path=path, line=line, radius=radius)
        return result

    def run_tests(self, command: str, timeout: int | None = None) -> dict[str, Any]:
        dangerous = ["rm -rf", "git reset", "git checkout", "shutdown", "reboot", "sudo "]
        if any(token in command for token in dangerous):
            raise ValueError("Refusing to run a destructive test command")
        result = subprocess.run(
            command,
            shell=True,
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=timeout or self.test_timeout,
        )
        output = truncate((result.stdout + result.stderr).strip())
        payload = {"command": command, "returncode": result.returncode, "output": output}
        self.transcript.emit("tool.run_tests", command=command, returncode=result.returncode)
        return payload

    def secret_scan(self) -> list[dict[str, Any]]:
        diff = self._diff_cache or self.git_diff()
        findings: list[dict[str, Any]] = []
        for added in iter_added_lines(diff):
            text = added["text"]
            if PRIVATE_KEY_RE.search(text) or SECRET_RE.search(text):
                findings.append(
                    {
                        "file": added["file"],
                        "line": added["line"],
                        "evidence": text.strip(),
                    }
                )
        self.transcript.emit("tool.secret_scan", count=len(findings))
        return findings

    def search_code(self, query: str, path: str | None = None, max_matches: int = 20) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("search_code query is required")
        root = safe_repo_path(self.repo, path or ".")
        max_matches = max(1, min(int(max_matches or 20), 100))
        try:
            pattern = re.compile(query)
        except re.error:
            pattern = re.compile(re.escape(query))
        excluded = {".git", ".review-agent", ".tmp", ".pytest_cache", "__pycache__", "node_modules", ".venv"}
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        matches: list[dict[str, Any]] = []
        for fp in candidates:
            if len(matches) >= max_matches:
                break
            if not fp.is_file():
                continue
            rel = fp.relative_to(self.repo)
            if any(part in excluded for part in rel.parts):
                continue
            if not (is_source_file(rel.as_posix()) or rel.suffix.lower() in {".md", ".txt", ".toml", ".yaml", ".yml", ".json"}):
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, text in enumerate(lines, start=1):
                if pattern.search(text):
                    matches.append({"file": rel.as_posix(), "line": number, "text": truncate(text.strip(), 500)})
                    if len(matches) >= max_matches:
                        break
        self.transcript.emit("tool.search_code", query=query, path=path or ".", count=len(matches))
        return matches

    def retrieve_company_policy(
        self,
        query: str,
        category: str | None = None,
        max_results: int = 3,
    ) -> list[dict[str, Any]]:
        if not self.company_kb:
            self.transcript.emit("tool.retrieve_company_policy.disabled", reason="company RAG is disabled")
            return []
        results = self.company_kb.search(query, category=category, max_results=max_results)
        self.transcript.emit("tool.retrieve_company_policy", count=len(results), category=category or "")
        return results

    def risk_scan(self) -> list[dict[str, Any]]:
        diff = self.git_diff()
        signals: list[dict[str, Any]] = []
        added = list(iter_added_lines(diff))
        added_by_file: dict[str, list[dict[str, Any]]] = {}
        for line in added:
            added_by_file.setdefault(line["file"], []).append(line)

        def add(line: dict[str, Any], category: str, signal: str, rationale: str) -> None:
            signals.append(
                {
                    "file": line["file"],
                    "line": line["line"],
                    "category": category,
                    "signal": signal,
                    "evidence": truncate(line["text"].strip(), 500),
                    "rationale": rationale,
                }
            )

        pending_execute: dict[str, dict[str, Any]] = {}
        pending_except: dict[str, dict[str, Any]] = {}
        for line in added:
            text = line["text"]
            stripped = text.strip()
            file_path = line["file"]
            lowered = stripped.lower()
            if ".execute(" in stripped:
                pending_execute[file_path] = line
            elif file_path in pending_execute and ('f"' in stripped or "f'" in stripped or "{" in stripped):
                add(
                    line,
                    "security",
                    "sql_interpolation",
                    "SQL text appears to interpolate variables across a multi-line execute call.",
                )
                pending_execute.pop(file_path, None)
            if re.search(r"\.execute\(f[\"']", stripped) or re.search(r"\.execute\(.*\+.*\)", stripped):
                add(line, "security", "sql_interpolation", "SQL query appears to interpolate values directly.")
            if "shell=True" in stripped or re.search(r"\bos\.system\(", stripped):
                add(line, "security", "shell_execution", "Shell execution is introduced on a path containing formatted values.")
            if re.match(r"def .*=\s*(\[\]|\{\})", stripped):
                add(line, "correctness", "mutable_default", "Mutable default arguments can share state across requests.")
            if re.match(r"except(?:\s+Exception)?\s*:$", stripped):
                pending_except[file_path] = line
            if re.match(r"except(?:\s+Exception)?\s*:\s*pass$", stripped) or (
                stripped == "pass" and pending_except.get(file_path, {}).get("line") == line["line"] - 1
            ):
                add(
                    pending_except.get(file_path, line),
                    "correctness",
                    "swallowed_exception",
                    "The new code swallows exceptions and can fail open or hide data failures.",
                )
            if re.search(r"\b(print|logging\.\w+)\(.*(token|secret|signature|card|password)", lowered) and (
                "{" in stripped or "=" in stripped
            ):
                add(line, "security", "sensitive_logging", "Sensitive values appear in logs or print output.")
            if "signature !=" in stripped and file_path in added_by_file:
                next_lines = [
                    item["text"].strip().lower()
                    for item in added_by_file[file_path]
                    if line["line"] < item["line"] <= line["line"] + 3
                ]
                rejects = ("reject", "unauthorized", "forbidden", "invalid", "401", "403", "raise")
                if not any(text.startswith("raise") or (text.startswith("return") and any(token in text for token in rejects)) for text in next_lines):
                    add(line, "security", "signature_mismatch_not_rejected", "Signature mismatch is observed but not rejected.")
            if "startswith(\"test-\")" in stripped or "startswith('test-')" in stripped:
                add(line, "correctness", "trust_bypass_pattern", "A test-prefix shortcut can accidentally trust production input.")
            if "high_risk" in lowered and "approved" in lowered:
                add(line, "correctness", "risk_rule_approves_high_risk_case", "High-risk payment logic returns approval.")
            if "amount = -abs(" in stripped:
                add(line, "correctness", "negative_amount_refund", "Refund handling creates negative amounts that may need downstream validation.")

        changed_files = self.changed_files()
        if any(is_source_file(item.get("file", "")) for item in changed_files) and not any(
            is_test_file(item.get("file", "")) for item in changed_files
        ):
            first_source = next(item for item in changed_files if is_source_file(item.get("file", "")))
            signals.append(
                {
                    "file": first_source["file"],
                    "line": 1,
                    "category": "testing",
                    "signal": "no_tests_changed",
                    "evidence": "No test files changed with this source diff.",
                    "rationale": "Risk-sensitive source changes should normally include targeted tests.",
                }
            )
            test_signal_map = {
                "risk_rule_approves_high_risk_case": "missing_test_high_risk_country_logic",
                "signature_mismatch_not_rejected": "missing_test_webhook_signature_mismatch",
                "negative_amount_refund": "missing_test_refund_amount_handling",
                "sql_interpolation": "missing_test_sql_parameterization_or_malicious_user_id",
                "shell_execution": "missing_test_shell_execution_input_handling",
                "swallowed_exception": "missing_test_failure_path",
            }
            for signal in list(signals):
                mapped = test_signal_map.get(signal["signal"])
                if not mapped:
                    continue
                signals.append(
                    {
                        "file": signal["file"],
                        "line": signal["line"],
                        "category": "testing",
                        "signal": mapped,
                        "evidence": signal["evidence"],
                        "rationale": f"No test files changed for critical behavior: {signal['rationale']}",
                    }
                )
        self.transcript.emit("tool.risk_scan", count=len(signals))
        return signals
