#!/usr/bin/env python3
"""Build the modular package from the untouched legacy implementation.

This migration helper is intentionally AST-based: it copies complete top-level
definitions, so decorators and multiline definitions stay intact.  Runtime code
in ``agent-rep`` does not import the legacy module.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_FILE = PROJECT_ROOT / "agents" / "review_agent.py"
TARGET_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = TARGET_ROOT / "pr_review_agent"

COMMON_IMPORTS = """\
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
"""

MODULES: dict[str, dict[str, object]] = {
    "settings.py": {
        "assignments": [
            "OUTPUT_DIR_NAME", "TRANSCRIPT_NAME", "REPORT_NAME", "FINDINGS_NAME",
            "JUDGE_INPUT_NAME", "JUDGE_NAME", "JUDGE_REPORT_NAME",
            "JUDGE_TRANSCRIPT_NAME", "COMPANY_KNOWLEDGE_INDEX_NAME", "MAX_DIFF_CHARS",
            "MAX_CMD_OUTPUT", "MAX_SKILL_CHARS", "MAX_COMPANY_CONTEXT_CHARS",
            "DASHSCOPE_BASE_URL", "LLM_PROVIDERS", "SEVERITIES", "CATEGORIES",
            "VERDICTS", "SECRET_RE", "PRIVATE_KEY_RE", "TOOLS",
        ],
        "preamble": "REPO_ROOT = Path(__file__).resolve().parent / 'resources'\n",
    },
    "utils.py": {
        "symbols": [
            "now", "truncate", "safe_repo_path", "run_git", "is_git_repo",
            "load_env_file", "git_range", "iter_review_files",
            "synthetic_changed_files", "synthetic_diff", "parse_numstat",
            "iter_added_lines", "is_test_file", "is_source_file",
        ],
        "imports": "from .settings import MAX_CMD_OUTPUT, MAX_DIFF_CHARS\n",
    },
    "domain.py": {
        "assignments": ["VALID_MESSAGE_TYPES"],
        "symbols": [
            "Finding", "Transcript", "FindingCollector", "CouncilMessage",
            "MessageBus", "EvidenceItem", "EvidenceStore", "CouncilFinding",
            "FindingLifecycle", "TodoManager",
        ],
        "imports": (
            "from .settings import CATEGORIES, SEVERITIES\n"
            "from .utils import now, truncate\n"
        ),
    },
    "skills.py": {"symbols": ["SkillLoader"]},
    "knowledge.py": {
        "symbols": ["CompanyKnowledgeChunk", "CompanyKnowledgeBase"],
        "imports": (
            "from .domain import Transcript\n"
            "from .settings import MAX_COMPANY_CONTEXT_CHARS\n"
            "from .utils import truncate\n"
        ),
    },
    "tools.py": {
        "symbols": ["ReviewTools"],
        "imports": (
            "from .domain import Transcript\n"
            "from .knowledge import CompanyKnowledgeBase\n"
            "from .settings import MAX_DIFF_CHARS, PRIVATE_KEY_RE, SECRET_RE\n"
            "from .utils import (git_range, is_git_repo, is_source_file, is_test_file,\n"
            "    iter_added_lines, parse_numstat, run_git, safe_repo_path,\n"
            "    synthetic_changed_files, synthetic_diff, truncate)\n"
        ),
    },
    "reviewers.py": {
        "symbols": ["SpecialtyReviewers"],
        "imports": (
            "from .domain import Finding, FindingCollector, Transcript\n"
            "from .settings import PRIVATE_KEY_RE, SECRET_RE\n"
            "from .tools import ReviewTools\n"
            "from .utils import is_source_file, is_test_file, iter_added_lines, truncate\n"
        ),
    },
    "llm.py": {
        "assignments": ["AliyunDashScopeClient"],
        "symbols": ["LLMClient", "OpenAICompatibleClient"],
        "imports": (
            "from .domain import CouncilFinding, Finding, FindingCollector, Transcript\n"
            "from .settings import MAX_DIFF_CHARS, MAX_SKILL_CHARS, SEVERITIES\n"
            "from .utils import truncate\n"
        ),
    },
    "orchestration.py": {
        "symbols": [
            "ReviewAgentMember", "CriticReviewer", "AgenticReviewLoop",
            "ReviewCouncil", "DebateCouncilLoop",
        ],
        "imports": (
            "from .domain import (CouncilFinding, EvidenceStore, Finding, FindingCollector,\n"
            "    FindingLifecycle, MessageBus, TodoManager, Transcript)\n"
            "from .llm import LLMClient\n"
            "from .reviewers import SpecialtyReviewers\n"
            "from .settings import TOOLS\n"
            "from .tools import ReviewTools\n"
            "from .utils import truncate\n"
        ),
    },
    "reporting.py": {
        "assignments": ["ZH_CATEGORY", "ZH_TITLE", "ZH_TEXT", "ReportWriter"],
        "symbols": ["zh_text", "ReportWriter", "ReportWriterAgent", "CouncilReportWriter"],
        "imports": (
            "from .domain import FindingCollector, Transcript\n"
            "from .llm import LLMClient\n"
            "from .settings import (FINDINGS_NAME, JUDGE_INPUT_NAME, REPORT_NAME,\n"
            "    SEVERITIES, VERDICTS)\n"
        ),
    },
    "judge.py": {
        "symbols": ["JudgeRunner"],
        "imports": (
            "from .domain import Transcript\n"
            "from .llm import LLMClient\n"
            "from .settings import JUDGE_NAME, JUDGE_REPORT_NAME\n"
            "from .utils import run_git, safe_repo_path\n"
        ),
    },
    "agent.py": {
        "symbols": ["ReviewAgent"],
        "imports": (
            "from .domain import FindingCollector, TodoManager, Transcript\n"
            "from .knowledge import CompanyKnowledgeBase\n"
            "from .llm import LLMClient, OpenAICompatibleClient\n"
            "from .orchestration import AgenticReviewLoop, DebateCouncilLoop, ReviewCouncil\n"
            "from .reporting import ReportWriter, ReportWriterAgent\n"
            "from .reviewers import SpecialtyReviewers\n"
            "from .settings import (COMPANY_KNOWLEDGE_INDEX_NAME, DASHSCOPE_BASE_URL,\n"
            "    LLM_PROVIDERS, OUTPUT_DIR_NAME, REPO_ROOT, TOOLS, TRANSCRIPT_NAME)\n"
            "from .skills import SkillLoader\n"
            "from .tools import ReviewTools\n"
            "from .utils import load_env_file, safe_repo_path, truncate\n"
        ),
    },
    "cli.py": {
        "symbols": ["build_parser", "main"],
        "imports": (
            "from .agent import ReviewAgent\n"
            "from .domain import Transcript\n"
            "from .judge import JudgeRunner\n"
            "from .llm import LLMClient, OpenAICompatibleClient\n"
            "from .settings import (JUDGE_TRANSCRIPT_NAME, LLM_PROVIDERS, OUTPUT_DIR_NAME)\n"
            "from .utils import load_env_file\n"
        ),
    },
}


def assignment_name(node: ast.Assign) -> str | None:
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        return None
    return node.targets[0].id


def source_for_node(source_lines: list[str], node: ast.AST) -> str:
    """Return a complete definition, including decorators omitted by AST spans."""
    decorator_lines = [item.lineno for item in getattr(node, "decorator_list", [])]
    start = min([node.lineno, *decorator_lines]) - 1
    return "".join(source_lines[start : node.end_lineno]).rstrip()


def main() -> None:
    source = LEGACY_FILE.read_text(encoding="utf-8")
    source_lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assignments = {
        name: node
        for node in tree.body
        if isinstance(node, ast.Assign) and (name := assignment_name(node))
    }

    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    for filename, spec in MODULES.items():
        requested: list[ast.AST] = []
        for name in spec.get("symbols", []):
            requested.append(definitions[name])
        for name in spec.get("assignments", []):
            requested.append(assignments[name])
        requested.sort(key=lambda node: node.lineno)
        chunks = [source_for_node(source_lines, node) for node in requested]
        doc = f'"""Modularized {filename[:-3]} component of the PR review agent."""\n\n'
        content = (
            doc
            + COMMON_IMPORTS
            + "\n"
            + str(spec.get("imports", ""))
            + str(spec.get("preamble", ""))
            + "\n\n"
            + "\n\n\n".join(chunks)
            + "\n"
        )
        (PACKAGE_ROOT / filename).write_text(content, encoding="utf-8")

    source_skill = PROJECT_ROOT / "skills" / "code-review" / "SKILL.md"
    target_skill = PACKAGE_ROOT / "resources" / "skills" / "code-review" / "SKILL.md"
    target_skill.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_skill, target_skill)


if __name__ == "__main__":
    main()
