"""Modularized cli component of the PR review agent."""

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

from .agent import ReviewAgent
from .domain import Transcript
from .judge import JudgeRunner
from .llm import LLMClient, OpenAICompatibleClient
from .settings import (JUDGE_TRANSCRIPT_NAME, LLM_PROVIDERS, OUTPUT_DIR_NAME)
from .utils import load_env_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local PR risk review agent.")
    parser.add_argument("--repo", default=".", help="Repository path to review.")
    parser.add_argument("--base", default="main", help="Base branch or revision.")
    parser.add_argument("--target", default="HEAD", help="Target branch or revision.")
    parser.add_argument("--pr-description", help="Optional PR description markdown file.")
    parser.add_argument("--test-command", help="Optional test command to run, e.g. 'python -m pytest'.")
    parser.add_argument("--language", choices=["zh", "en"], default="zh", help="Report language.")
    parser.add_argument("--mode", choices=["debate", "council", "agentic", "simple"], default="debate", help="Review execution mode.")
    parser.add_argument("--llm-provider", choices=["aliyun", "deepseek", "none"], default="aliyun", help="LLM provider for specialist reviewers.")
    parser.add_argument("--llm-model", help="Chat model; defaults to the selected provider's model.")
    parser.add_argument("--llm-base-url", help="OpenAI-compatible chat API base URL; defaults to the selected provider's URL.")
    parser.add_argument("--company-knowledge-dir", default="knowledge/company", help="Directory of company policy Markdown files for RAG alignment.")
    parser.add_argument("--embedding-model", default="text-embedding-v4", help="DashScope embedding model for company policy RAG.")
    parser.add_argument("--embedding-dimensions", type=int, default=1024, help="Embedding dimensions for company policy RAG.")
    parser.add_argument("--disable-company-rag", action="store_true", help="Disable company policy RAG alignment.")
    parser.add_argument("--debate-max-actions", type=int, default=12, help="Maximum dynamic actions for debate council mode.")
    parser.add_argument("--judge-report", help="Run AI judge on a standardized judge_input.json instead of running review.")
    parser.add_argument("--judge-model", help="AI judge model; defaults to the selected provider's judge model.")
    parser.add_argument(
        "--critic-pass",
        choices=["true", "false"],
        default="true",
        help="Whether critic-reviewer challenges candidate findings in council mode.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    pr_description = Path(args.pr_description) if args.pr_description else None
    load_env_file(repo / ".env")
    if args.judge_report:
        output_dir = repo / OUTPUT_DIR_NAME
        transcript = Transcript(output_dir / JUDGE_TRANSCRIPT_NAME)
        llm_client: LLMClient | None
        if args.llm_provider == "none":
            llm_client = None
            transcript.emit("llm.disabled", provider="none")
        else:
            provider_defaults = LLM_PROVIDERS[args.llm_provider]
            llm_client = OpenAICompatibleClient(
                api_key=os.getenv(provider_defaults["api_key_env"]),
                model=args.judge_model or provider_defaults["judge_model"],
                base_url=args.llm_base_url or provider_defaults["base_url"],
                transcript=transcript,
                provider=args.llm_provider,
            )
            transcript.emit(
                "llm.configure",
                provider=llm_client.provider,
                model=llm_client.model,
                base_url=llm_client.base_url,
                enabled=llm_client.enabled,
            )
        paths = JudgeRunner(repo, output_dir, transcript, llm_client).run(
            Path(args.judge_report),
            args.base,
            args.target,
            pr_description,
        )
        print(f"[judge] output: {paths['judge']}")
        print(f"[judge] report: {paths['judge_report']}")
        return 0
    agent = ReviewAgent(
        repo=repo,
        base=args.base,
        target=args.target,
        pr_description=pr_description,
        test_command=args.test_command,
        language=args.language,
        mode=args.mode,
        critic_pass=args.critic_pass == "true",
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
        debate_max_actions=args.debate_max_actions,
        company_knowledge_dir=repo / args.company_knowledge_dir,
        embedding_model=args.embedding_model,
        embedding_dimensions=args.embedding_dimensions,
        disable_company_rag=args.disable_company_rag,
    )
    result = agent.run()
    print(f"[agent] verdict: {result['verdict']}")
    print(f"[agent] summary: {result['summary']}")
    print(f"[agent] report: {result['paths']['report']}")
    print(f"[agent] findings: {result['paths']['findings']}")
    return 0
