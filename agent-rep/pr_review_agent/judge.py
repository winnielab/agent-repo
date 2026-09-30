"""Modularized judge component of the PR review agent."""

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
from .llm import LLMClient
from .settings import JUDGE_NAME, JUDGE_REPORT_NAME
from .utils import run_git, safe_repo_path


class JudgeRunner:
    def __init__(
        self,
        repo: Path,
        output_dir: Path,
        transcript: Transcript,
        llm_client: LLMClient | None,
    ):
        self.repo = repo
        self.output_dir = output_dir
        self.transcript = transcript
        self.llm_client = llm_client
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        judge_report: Path,
        base: str,
        target: str,
        pr_description: Path | None = None,
    ) -> dict[str, str]:
        path = judge_report if judge_report.is_absolute() else safe_repo_path(self.repo, str(judge_report))
        judge_input = json.loads(path.read_text(encoding="utf-8"))
        diff = self._git_diff(base, target)
        description = ""
        if pr_description:
            description_path = pr_description if pr_description.is_absolute() else safe_repo_path(self.repo, str(pr_description))
            description = description_path.read_text(encoding="utf-8", errors="replace")
        raw = self.llm_client.judge_report(judge_input, diff, description) if self.llm_client and self.llm_client.enabled else {}
        result = self._normalize(raw)
        judge_path = self.output_dir / JUDGE_NAME
        markdown_path = self.output_dir / JUDGE_REPORT_NAME
        judge_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        markdown_path.write_text(self._markdown(result), encoding="utf-8")
        self.transcript.emit("judge.complete", score=result["overall_score"], verdict=result["verdict"])
        return {"judge": str(judge_path), "judge_report": str(markdown_path)}

    def _git_diff(self, base: str, target: str) -> str:
        try:
            return run_git(self.repo, ["diff", "--no-ext-diff", "--unified=80", base, target], timeout=60)
        except Exception as exc:
            self.transcript.emit("judge.diff_error", error=str(exc))
            return ""

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        dimensions = {
            "critical_issue_coverage": 0,
            "evidence_quality": 0,
            "severity_accuracy": 0,
            "duplicate_noise_control": 0,
            "actionability": 0,
            "report_clarity": 0,
        }
        if isinstance(payload.get("dimensions"), dict):
            for key in dimensions:
                try:
                    dimensions[key] = max(0, min(int(payload["dimensions"].get(key, 0)), 100))
                except (TypeError, ValueError):
                    dimensions[key] = 0
        try:
            overall = int(payload.get("overall_score", 0))
        except (TypeError, ValueError):
            overall = 0
        overall = max(0, min(overall, 100))
        verdict = str(payload.get("verdict", "needs_improvement")).strip()
        if verdict not in {"pass", "needs_improvement", "fail"}:
            verdict = "pass" if overall >= 80 else "needs_improvement"
        return {
            "overall_score": overall,
            "verdict": verdict,
            "dimensions": dimensions,
            "strengths": self._list_of_strings(payload.get("strengths")),
            "weaknesses": self._list_of_strings(payload.get("weaknesses")),
            "recommendations": self._list_of_strings(payload.get("recommendations")),
        }

    def _list_of_strings(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()]

    def _markdown(self, result: dict[str, Any]) -> str:
        lines = [
            "# AI Judge Review Quality Report",
            "",
            f"**Overall Score:** {result['overall_score']}",
            f"**Verdict:** `{result['verdict']}`",
            "",
            "## Dimensions",
            "",
        ]
        for key, score in result["dimensions"].items():
            lines.append(f"- `{key}`: {score}")
        for key, title in (
            ("strengths", "Strengths"),
            ("weaknesses", "Weaknesses"),
            ("recommendations", "Recommendations"),
        ):
            lines.extend(["", f"## {title}", ""])
            items = result.get(key) or []
            lines.extend(f"- {item}" for item in items) if items else lines.append("- None.")
        return "\n".join(lines).rstrip() + "\n"
