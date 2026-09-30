"""Adapter from RepoPilot review contracts to the existing PR review agent."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from repopilot.core.models import Serializable
from repopilot.planning.plan import Plan
from repopilot.task.intent import requests_test_changes
from repopilot.task.specification import TaskSpecification


@dataclass(frozen=True)
class ReviewInput(Serializable):
    specification: TaskSpecification
    plan: Plan
    workspace: Path
    diff: str
    test_result: Dict[str, Any]
    test_command: str
    test_timeout_seconds: float = 180.0


@dataclass(frozen=True)
class ReviewIssue(Serializable):
    title: str
    severity: str
    file: str = ""
    line: int = 0
    evidence: str = ""
    suggestion: str = ""


@dataclass(frozen=True)
class ReviewResult(Serializable):
    decision: str
    issues: List[ReviewIssue] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    raw_artifacts: Dict[str, Any] = field(default_factory=dict)


class PRReviewAdapter:
    def __init__(self, artifact_dir: Path, mode: str = "simple",
                 runner: Optional[Callable[[ReviewInput], Dict[str, Any]]] = None) -> None:
        if mode not in {"simple", "council"}:
            raise ValueError("Stage 2 reviewer mode must be simple or council")
        self.artifact_dir = Path(artifact_dir)
        self.mode = mode
        self.runner = runner

    @staticmethod
    def _run(args: List[str], cwd: Path, input_text: Optional[str] = None) -> None:
        result = subprocess.run(args, cwd=str(cwd), input=input_text, text=True,
                                capture_output=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    @staticmethod
    def _review_agent_class():
        try:
            from pr_review_agent import ReviewAgent
            return ReviewAgent
        except ImportError:
            legacy_root = Path(__file__).resolve().parents[2] / "agent-rep"
            sys.path.insert(0, str(legacy_root))
            from pr_review_agent import ReviewAgent
            return ReviewAgent

    def _legacy_run(self, review_input: ReviewInput) -> Dict[str, Any]:
        if not review_input.diff.strip():
            return {"verdict": "request_changes", "summary": "No patch to review", "findings": [], "paths": {}}
        with tempfile.TemporaryDirectory(prefix="repopilot-review-") as raw:
            root = Path(raw)
            repo = root / "repo"
            self._run(["git", "clone", "--quiet", "--no-hardlinks", str(review_input.workspace), str(repo)], root)
            self._run(["git", "apply", "--whitespace=nowarn", "-"], repo, review_input.diff)
            self._run(["git", "add", "--all"], repo)
            self._run(["git", "-c", "user.name=RepoPilot", "-c", "user.email=repopilot@example.invalid",
                       "commit", "--quiet", "-m", "RepoPilot candidate patch"], repo)
            description = root / "issue.md"
            description.write_text(json.dumps({
                "task": review_input.specification.to_dict(),
                "plan": review_input.plan.to_dict(),
                "test_result": review_input.test_result,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            ReviewAgent = self._review_agent_class()
            payload = ReviewAgent(
                repo=repo, base="HEAD~1", target="HEAD", pr_description=description,
                test_command=review_input.test_command,
                test_result=review_input.test_result,
                test_timeout=max(1, int(review_input.test_timeout_seconds)),
                language="en", mode=self.mode,
                critic_pass=False, llm_provider="none", disable_company_rag=True,
            ).run()
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            copied = {}
            for name, source in payload.get("paths", {}).items():
                source_path = Path(source)
                if source_path.is_file():
                    destination = self.artifact_dir / f"pr-review-{source_path.name}"
                    shutil.copy2(source_path, destination)
                    copied[name] = str(destination)
            return {**payload, "paths": copied, "test_evidence_source": "precomputed"}

    def review(self, review_input: ReviewInput) -> ReviewResult:
        payload = self.runner(review_input) if self.runner else self._legacy_run(review_input)
        raw_findings = payload.get("findings", [])
        issues = [ReviewIssue(
            title=str(item.get("title", "Review issue")), severity=str(item.get("severity", "P2")),
            file=str(item.get("file", "")), line=int(item.get("line", 0) or 0),
            evidence=str(item.get("evidence", ""))[-4_000:],
            suggestion=str(item.get("suggestion", ""))[:2_000],
        ) for item in raw_findings if isinstance(item, dict)]
        requests_tests = requests_test_changes(review_input.specification)
        suppressed: List[ReviewIssue] = []
        retained: List[ReviewIssue] = []
        for issue in issues:
            normalized_title = issue.title.casefold()
            effective_test_false_positive = (
                bool(review_input.test_result.get("ok"))
                and normalized_title == "configured test command failed"
            )
            unrequested_test_change = (
                not requests_tests
                and normalized_title == "production code changed without nearby test changes"
            )
            if effective_test_false_positive or unrequested_test_change:
                suppressed.append(issue)
            else:
                retained.append(issue)
        issues = retained
        verdict = str(payload.get("verdict", "request_changes"))
        decision = "reject" if issues or (verdict == "request_changes" and not suppressed) else "approve"
        return ReviewResult(decision, issues, [issue.suggestion for issue in issues if issue.suggestion],
                            {"legacy_verdict": verdict, "paths": payload.get("paths", {}),
                             "summary": payload.get("summary", ""),
                             "test_evidence_source": payload.get("test_evidence_source", "runner"),
                             "suppressed_findings": [item.to_dict() for item in suppressed]})
