"""Modularized agent component of the PR review agent."""

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

from .domain import FindingCollector, TodoManager, Transcript
from .knowledge import CompanyKnowledgeBase
from .llm import LLMClient, OpenAICompatibleClient
from .orchestration import AgenticReviewLoop, DebateCouncilLoop, ReviewCouncil
from .reporting import ReportWriter, ReportWriterAgent
from .reviewers import SpecialtyReviewers
from .settings import (COMPANY_KNOWLEDGE_INDEX_NAME, DASHSCOPE_BASE_URL,
    LLM_PROVIDERS, OUTPUT_DIR_NAME, REPO_ROOT, TOOLS, TRANSCRIPT_NAME)
from .skills import SkillLoader
from .tools import ReviewTools
from .utils import load_env_file, safe_repo_path, truncate


class ReviewAgent:
    def __init__(
        self,
        repo: Path,
        base: str,
        target: str,
        pr_description: Path | None = None,
        test_command: str | None = None,
        language: str = "zh",
        mode: str = "debate",
        critic_pass: bool = True,
        llm_provider: str = "aliyun",
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        debate_max_actions: int = 12,
        company_knowledge_dir: Path | None = None,
        embedding_model: str = "text-embedding-v4",
        embedding_dimensions: int = 1024,
        disable_company_rag: bool = False,
    ):
        self.repo = repo.resolve()
        self.base = base
        self.target = target
        self.pr_description = pr_description
        self.test_command = test_command
        self.language = language
        self.mode = mode
        self.critic_pass = critic_pass
        self.llm_provider = llm_provider
        provider_defaults = LLM_PROVIDERS[llm_provider if llm_provider != "none" else "aliyun"]
        self.llm_model = llm_model or provider_defaults["model"]
        self.llm_base_url = llm_base_url or provider_defaults["base_url"]
        self.debate_max_actions = debate_max_actions
        self.company_knowledge_dir = company_knowledge_dir or (self.repo / "knowledge" / "company")
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.disable_company_rag = disable_company_rag
        self.output_dir = self.repo / OUTPUT_DIR_NAME
        self.transcript = Transcript(self.output_dir / TRANSCRIPT_NAME)
        self.collector = FindingCollector()
        self.todos = TodoManager()
        self.skills = SkillLoader(REPO_ROOT / "skills")
        self.skill_context = ""
        self.loaded_env_keys = load_env_file(self.repo / ".env")
        if self.loaded_env_keys:
            self.transcript.emit("env.load", path=".env", keys=self.loaded_env_keys)
        self.company_kb = self._build_company_kb()
        self.tools = ReviewTools(self.repo, self.base, self.target, self.transcript, self.company_kb)
        self.llm_client = self._build_llm_client()
        self.reviewers = SpecialtyReviewers(self.tools, self.collector, self.transcript)
        self.council_result: dict[str, Any] = {"findings": [], "messages": []}
        self.standardized_report: dict[str, Any] = {}
        self.tool_handlers: dict[str, Callable[..., Any]] = {
            "git_diff": self.tools.git_diff,
            "changed_files": self.tools.changed_files,
            "read_file_context": self.tools.read_file_context,
            "run_tests": self.tools.run_tests,
            "secret_scan": self.tools.secret_scan,
            "search_code": self.tools.search_code,
            "risk_scan": self.tools.risk_scan,
            "retrieve_company_policy": self.tools.retrieve_company_policy,
            "emit_finding": self.collector.emit,
            "write_report": lambda path=None: ReportWriter(
                self.output_dir,
                self.collector,
                self.language,
                self.council_result.get("findings", []),
                self.council_result.get("messages", []),
                self.standardized_report,
            ).write(),
        }

    def _build_company_kb(self) -> CompanyKnowledgeBase | None:
        if self.disable_company_rag:
            self.transcript.emit("company_rag.disabled", reason="--disable-company-rag")
            return None
        return CompanyKnowledgeBase(
            knowledge_dir=self.company_knowledge_dir,
            index_path=self.output_dir / COMPANY_KNOWLEDGE_INDEX_NAME,
            transcript=self.transcript,
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url=DASHSCOPE_BASE_URL,
            embedding_model=self.embedding_model,
            embedding_dimensions=self.embedding_dimensions,
            enabled=True,
        )

    def _build_llm_client(self) -> LLMClient | None:
        if self.llm_provider == "none":
            self.transcript.emit("llm.disabled", provider="none")
            return None
        if self.llm_provider in LLM_PROVIDERS:
            client = OpenAICompatibleClient(
                api_key=os.getenv(LLM_PROVIDERS[self.llm_provider]["api_key_env"]),
                model=self.llm_model,
                base_url=self.llm_base_url,
                transcript=self.transcript,
                provider=self.llm_provider,
            )
            self.transcript.emit(
                "llm.configure",
                provider=client.provider,
                model=client.model,
                base_url=client.base_url,
                enabled=client.enabled,
            )
            return client
        raise ValueError(f"Unsupported LLM provider: {self.llm_provider}")

    def _description_text(self) -> str:
        if not self.pr_description:
            return ""
        path = self.pr_description
        if not path.is_absolute():
            path = safe_repo_path(self.repo, str(path))
        return path.read_text(encoding="utf-8", errors="replace")

    def _augment_skill_context(self, skill: str, query: str) -> str:
        if not self.company_kb:
            return skill
        block = self.company_kb.context_block(query, max_results=5)
        if not block:
            return skill
        return f"{skill}\n\n{block}"

    def run(self) -> dict[str, Any]:
        self.transcript.emit(
            "review.start",
            repo=str(self.repo),
            base=self.base,
            target=self.target,
            mode=self.mode,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
            tools=[tool["name"] for tool in TOOLS],
            skills=self.skills.descriptions(),
        )
        self.todos.update(
            [
                {"content": "Load code-review skill and understand PR context", "status": "completed"},
                {"content": "Inspect changed files and diff", "status": "in_progress"},
                {"content": "Run specialist review agents", "status": "pending"},
                {"content": "Write Markdown and JSON report", "status": "pending"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)

        skill = self.skills.load("code-review")
        self.skill_context = skill
        pr_description = self._description_text()
        self.transcript.emit("skill.load", name="code-review", chars=len(skill))
        if pr_description:
            self.transcript.emit("pr.description", chars=len(pr_description))

        if self.mode == "agentic":
            self.skill_context = self._augment_skill_context(skill, pr_description)
            loop = AgenticReviewLoop(
                tools=self.tools,
                transcript=self.transcript,
                collector=self.collector,
                todos=self.todos,
                critic_pass=self.critic_pass,
                llm_client=self.llm_client,
                skill_context=self.skill_context,
                test_command=self.test_command,
            )
            print("[agent] starting agentic review loop")
            self.council_result = loop.run(pr_description)
            print(f"[agent] agentic loop accepted {len(self.collector.findings)} finding(s)")
        else:
            files = self.tools.changed_files()
            diff = self.tools.git_diff()
            test_result = self.tools.run_tests(self.test_command) if self.test_command else None
            self.skill_context = self._augment_skill_context(
                skill,
                "\n".join([pr_description, json.dumps(files, ensure_ascii=False), truncate(diff, 12000)]),
            )

            self.todos.update(
                [
                    {"content": "Load code-review skill and understand PR context", "status": "completed"},
                    {"content": "Inspect changed files and diff", "status": "completed"},
                    {"content": "Run specialist review agents", "status": "in_progress"},
                    {"content": "Write Markdown and JSON report", "status": "pending"},
                ]
            )
            self.transcript.emit("todo.update", todos=self.todos.items)

            if self.mode == "simple":
                for reviewer in ("security-reviewer", "correctness-reviewer", "test-reviewer", "company-policy-reviewer"):
                    print(f"[agent] spawning {reviewer}")
                    print("[agent] " + self.reviewers.run(reviewer, diff, files, test_result))
            elif self.mode == "debate":
                debate = DebateCouncilLoop(
                    tools=self.tools,
                    transcript=self.transcript,
                    collector=self.collector,
                    critic_pass=self.critic_pass,
                    llm_client=self.llm_client,
                    skill_context=self.skill_context,
                    max_actions=self.debate_max_actions,
                    language=self.language,
                )
                print("[agent] starting debate council loop")
                self.council_result = debate.run(diff, files, test_result, pr_description)
                print(
                    f"[agent] debate council accepted {len(self.collector.findings)} finding(s) "
                    f"from {len(self.council_result['findings'])} candidate(s)"
                )
            else:
                council = ReviewCouncil(
                    tools=self.tools,
                    transcript=self.transcript,
                    collector=self.collector,
                    critic_pass=self.critic_pass,
                    llm_client=self.llm_client,
                    skill_context=self.skill_context,
                )
                print("[agent] convening review council")
                self.council_result = council.run(diff, files, test_result, pr_description)
                print(
                    f"[agent] council accepted {len(self.collector.findings)} finding(s) "
                    f"from {len(self.council_result['findings'])} candidate(s)"
                )

        if self.mode == "agentic":
            final_todos = self.todos.items or [
                {"content": "Run agentic review loop", "status": "completed"},
                {"content": "Write Markdown and JSON report", "status": "in_progress"},
            ]
            if final_todos:
                final_todos = [
                    {
                        "content": item["content"],
                        "status": "completed" if item["status"] == "in_progress" else item["status"],
                    }
                    for item in final_todos
                ]
                if not any(item["content"] == "Write Markdown and JSON report" for item in final_todos):
                    final_todos.append({"content": "Write Markdown and JSON report", "status": "in_progress"})
                else:
                    final_todos = [
                        {
                            "content": item["content"],
                            "status": "in_progress" if item["content"] == "Write Markdown and JSON report" else item["status"],
                        }
                        for item in final_todos
                    ]
                self.todos.update(final_todos)
                self.transcript.emit("todo.update", todos=self.todos.items)
        else:
            self.todos.update(
                [
                    {"content": "Load code-review skill and understand PR context", "status": "completed"},
                    {"content": "Inspect changed files and diff", "status": "completed"},
                    {"content": "Run specialist review agents", "status": "completed"},
                    {"content": "Write Markdown and JSON report", "status": "in_progress"},
                ]
            )
            self.transcript.emit("todo.update", todos=self.todos.items)

        report_context = {
            "mode": self.mode,
            "council_records": self.council_result.get("findings", []),
            "council_messages": self.council_result.get("messages", []),
            "report_writer_notes": self.council_result.get("report_writer_notes", {}),
        }
        self.standardized_report = ReportWriterAgent(
            self.llm_client,
            self.transcript,
            self.language,
        ).draft(report_context)

        writer = ReportWriter(
            self.output_dir,
            self.collector,
            self.language,
            self.council_result.get("findings", []),
            self.council_result.get("messages", []),
            self.standardized_report,
        )
        paths = writer.write()
        payload = writer.payload()
        self.transcript.emit("review.complete", verdict=payload["verdict"], findings=len(payload["findings"]))

        if self.mode == "agentic":
            completed = [
                {"content": item["content"], "status": "completed"}
                for item in self.todos.items
                if item.get("content") != "Write Markdown and JSON report"
            ]
            completed.append({"content": "Write Markdown and JSON report", "status": "completed"})
            self.todos.update(completed)
        else:
            self.todos.update(
                [
                    {"content": "Load code-review skill and understand PR context", "status": "completed"},
                    {"content": "Inspect changed files and diff", "status": "completed"},
                    {"content": "Run specialist review agents", "status": "completed"},
                    {"content": "Write Markdown and JSON report", "status": "completed"},
                ]
            )
        self.transcript.emit("todo.update", todos=self.todos.items)
        return {**payload, "paths": paths}
