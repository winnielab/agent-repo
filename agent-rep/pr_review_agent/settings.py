"""Modularized settings component of the PR review agent."""

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

REPO_ROOT = Path(__file__).resolve().parent / 'resources'


OUTPUT_DIR_NAME = ".review-agent"


TRANSCRIPT_NAME = "transcript.jsonl"


REPORT_NAME = "report.md"


FINDINGS_NAME = "findings.json"


JUDGE_INPUT_NAME = "judge_input.json"


JUDGE_NAME = "judge.json"


JUDGE_REPORT_NAME = "judge.md"


JUDGE_TRANSCRIPT_NAME = "judge-transcript.jsonl"


COMPANY_KNOWLEDGE_INDEX_NAME = "company_knowledge_index.json"


MAX_DIFF_CHARS = 120000


MAX_CMD_OUTPUT = 50000


MAX_SKILL_CHARS = 12000


MAX_COMPANY_CONTEXT_CHARS = 6000


DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


LLM_PROVIDERS = {
    "aliyun": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "model": "qwen-turbo-latest",
        "base_url": DASHSCOPE_BASE_URL,
        "judge_model": "qwen-plus",
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "judge_model": "deepseek-flash",
    },
}


SEVERITIES = {"P0", "P1", "P2", "P3"}


CATEGORIES = {"security", "correctness", "performance", "testing", "maintainability"}


VERDICTS = {"approve", "comment", "request_changes"}


SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?(?:key|token)|secret|token|password)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{8,})"
)


PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")


TOOLS = [
    {
        "name": "git_diff",
        "description": "Get the diff between base and target revisions.",
        "input_schema": {
            "type": "object",
            "properties": {"base": {"type": "string"}, "target": {"type": "string"}},
            "required": ["base", "target"],
        },
    },
    {
        "name": "changed_files",
        "description": "List changed files with added/deleted line counts.",
        "input_schema": {
            "type": "object",
            "properties": {"base": {"type": "string"}, "target": {"type": "string"}},
            "required": ["base", "target"],
        },
    },
    {
        "name": "read_file_context",
        "description": "Read a small window of source context around a line.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line": {"type": "integer"},
                "radius": {"type": "integer"},
            },
            "required": ["path", "line"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run a read-only test command and return truncated output.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "secret_scan",
        "description": "Scan changed files and added diff lines for likely secrets.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_code",
        "description": "Search text files in the repository for a query or regex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "max_matches": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "risk_scan",
        "description": "Extract review-worthy risk signals from added diff lines for the LLM to judge.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "retrieve_company_policy",
        "description": "Retrieve company-specific coding, security, payment, and testing policies relevant to a review question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "emit_finding",
        "description": "Record one structured review finding.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "severity": {"type": "string", "enum": sorted(SEVERITIES)},
                "category": {"type": "string", "enum": sorted(CATEGORIES)},
                "title": {"type": "string"},
                "evidence": {"type": "string"},
                "impact": {"type": "string"},
                "suggestion": {"type": "string"},
            },
            "required": [
                "file",
                "line",
                "severity",
                "category",
                "title",
                "evidence",
                "impact",
                "suggestion",
            ],
        },
    },
    {
        "name": "write_report",
        "description": "Write Markdown and JSON review outputs.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
]
