"""Modularized orchestration component of the PR review agent."""

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

from .domain import (CouncilFinding, EvidenceStore, Finding, FindingCollector,
    FindingLifecycle, MessageBus, TodoManager, Transcript)
from .llm import LLMClient
from .reviewers import SpecialtyReviewers
from .settings import TOOLS
from .tools import ReviewTools
from .utils import truncate


class ReviewAgentMember:
    def __init__(
        self,
        name: str,
        role: str,
        tools: ReviewTools,
        transcript: Transcript,
        llm_client: LLMClient | None = None,
    ):
        self.name = name
        self.role = role
        self.tools = tools
        self.transcript = transcript
        self.llm_client = llm_client

    def review(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        focus: str = "",
        skill_context: str = "",
    ) -> list[Finding]:
        collector = FindingCollector()
        if self.llm_client:
            for finding in self.llm_client.review(
                self.name,
                self.role,
                focus,
                skill_context,
                diff,
                files,
                test_result,
            ):
                collector.emit(finding)
        reviewers = SpecialtyReviewers(self.tools, collector, self.transcript)
        reviewers.run(self.name, diff, files, test_result)
        return collector.sorted()


class CriticReviewer:
    def __init__(
        self,
        bus: MessageBus,
        evidence: EvidenceStore,
        lifecycle: FindingLifecycle,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
    ):
        self.name = "critic-reviewer"
        self.bus = bus
        self.evidence = evidence
        self.lifecycle = lifecycle
        self.llm_client = llm_client
        self.skill_context = skill_context
        self._challenged_once = False

    def review(self, items: list[CouncilFinding]) -> None:
        for item in items:
            chain = self.evidence.list(item.finding_id)
            decision = (
                self.llm_client.critique_finding(item, chain, self.skill_context)
                if self.llm_client
                else {}
            )
            if decision:
                self.evidence.add(
                    item.finding_id,
                    "critic_review",
                    f"{decision['decision']}: {decision['reason']}",
                    self.name,
                )
                should_challenge = decision["decision"] == "challenge"
                reason = decision["reason"]
            else:
                should_challenge = False
                reason = ""
                if item.finding.severity in {"P0", "P1"} and not self._challenged_once:
                    should_challenge = True
                    reason = "Merge-blocking severity must be backed by concrete diff evidence and source context."
                    self._challenged_once = True
                elif len(chain) < 2:
                    should_challenge = True
                    reason = "Finding has too little evidence for a final report."

            if should_challenge:
                self.lifecycle.challenge(item.finding_id, self.name, reason)
                self.evidence.add(item.finding_id, "critic_challenge", reason, self.name)
                self.bus.send(
                    self.name,
                    item.proposed_by,
                    "challenge",
                    reason,
                    item.finding_id,
                )
            else:
                self.bus.send(
                    self.name,
                    "lead-reviewer",
                    "resolution",
                    "No challenge; evidence is sufficient.",
                    item.finding_id,
                )


class AgenticReviewLoop:
    def __init__(
        self,
        tools: ReviewTools,
        transcript: Transcript,
        collector: FindingCollector,
        todos: TodoManager,
        critic_pass: bool = True,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
        test_command: str | None = None,
        max_steps: int = 20,
    ):
        self.tools = tools
        self.transcript = transcript
        self.collector = collector
        self.todos = todos
        self.critic_pass = critic_pass
        self.llm_client = llm_client
        self.skill_context = skill_context
        self.test_command = test_command
        self.max_steps = max_steps
        self.bus = MessageBus(transcript)
        self.evidence = EvidenceStore(transcript)
        self.lifecycle = FindingLifecycle(transcript)
        self.critic = CriticReviewer(self.bus, self.evidence, self.lifecycle, llm_client, skill_context)
        self.observations: list[dict[str, Any]] = []

    def run(self, pr_description: str = "") -> dict[str, Any]:
        self.transcript.emit("agentic.start", max_steps=self.max_steps)
        self._set_default_todos()
        if not self.llm_client or not self.llm_client.enabled:
            self.transcript.emit("agentic.fallback", reason="llm unavailable")
            return self._run_local_guardrails()

        finalized = False
        for step in range(1, self.max_steps + 1):
            action = self.llm_client.next_action(
                self._agent_state(pr_description, step),
                self.skill_context,
                self._available_tools(),
                self.observations,
            )
            if not action:
                action = self._recovery_action()
                self.transcript.emit("agent.recovery_action", step=step, **action)
            self.transcript.emit("agent.action", step=step, **action)
            observation = self._apply_action(action)
            self.observations.append(observation)
            self.transcript.emit("agent.observation", step=step, **observation)
            if action["action"] == "finalize":
                finalized = True
                break

        if not finalized:
            self.transcript.emit("agent.finalize", reason="max steps reached")
        self._coverage_review(pr_description)
        self._resolve_candidates("Agentic review finalized.")
        records = [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()]
        self.transcript.emit("agentic.complete", candidates=len(records), accepted=len(self.lifecycle.accepted()))
        return {"findings": records, "messages": self.bus.all()}

    def _recovery_action(self) -> dict[str, Any]:
        called_tools = [obs.get("tool") for obs in self.observations if obs.get("type") == "tool"]
        if "changed_files" not in called_tools:
            return {
                "thought": "Recover from invalid LLM action by inspecting changed files.",
                "action": "call_tool",
                "tool": "changed_files",
                "args": {},
            }
        if "git_diff" not in called_tools:
            return {
                "thought": "Recover from invalid LLM action by reading the diff.",
                "action": "call_tool",
                "tool": "git_diff",
                "args": {},
            }
        return {
            "thought": "Recover from repeated invalid LLM action by finalizing with gathered evidence.",
            "action": "finalize",
            "reason": "LLM returned invalid actions after useful observations were gathered.",
        }

    def _set_default_todos(self) -> None:
        self.todos.update(
            [
                {"content": "Inspect PR context with read-only tools", "status": "in_progress"},
                {"content": "Investigate risky changes and collect evidence", "status": "pending"},
                {"content": "Ask critic for weak or merge-blocking findings", "status": "pending"},
                {"content": "Finalize accepted findings and write report", "status": "pending"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)

    def _available_tools(self) -> list[dict[str, Any]]:
        allowed = {"git_diff", "changed_files", "read_file_context", "run_tests", "secret_scan", "search_code", "risk_scan"}
        return [tool for tool in TOOLS if tool["name"] in allowed]

    def _agent_state(self, pr_description: str, step: int) -> dict[str, Any]:
        return {
            "step": step,
            "max_steps": self.max_steps,
            "repo": str(self.tools.repo),
            "base": self.tools.base,
            "target": self.tools.target,
            "pr_description": pr_description[:6000],
            "todos": self.todos.items,
            "candidate_findings": [
                item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()
            ],
            "test_command_available": bool(self.test_command),
        }

    def _apply_action(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action["action"]
        try:
            if kind == "update_todos":
                rendered = self.todos.update(action["todos"])
                self.transcript.emit("todo.update", todos=self.todos.items)
                return {"type": "todo_update", "ok": True, "result": rendered}
            if kind == "call_tool":
                return self._call_tool(action["tool"], action.get("args", {}))
            if kind == "emit_finding":
                item = self._emit_candidate(action["finding"], proposed_by="agentic-reviewer")
                return {"type": "finding", "ok": True, "finding_id": item.finding_id}
            if kind == "ask_critic":
                return self._ask_critic(action)
            if kind == "finalize":
                self.transcript.emit("agent.finalize", reason=action.get("reason", "Agent finalized."))
                return {"type": "finalize", "ok": True, "reason": action.get("reason", "")}
        except Exception as exc:
            return {"type": kind, "ok": False, "error": str(exc)}
        return {"type": kind, "ok": False, "error": "Unknown action"}

    def _call_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "git_diff":
            result = self.tools.git_diff(args.get("base"), args.get("target"))
        elif tool == "changed_files":
            result = self.tools.changed_files(args.get("base"), args.get("target"))
        elif tool == "read_file_context":
            result = self.tools.read_file_context(
                str(args.get("path", "")),
                int(args.get("line", 1)),
                int(args.get("radius", 4)),
            )
        elif tool == "run_tests":
            if not self.test_command:
                return {
                    "type": "tool",
                    "ok": False,
                    "tool": tool,
                    "error": "run_tests is disabled because --test-command was not provided",
                }
            result = self.tools.run_tests(self.test_command)
        elif tool == "secret_scan":
            result = self.tools.secret_scan()
        elif tool == "search_code":
            result = self.tools.search_code(
                str(args.get("query", "")),
                args.get("path"),
                int(args.get("max_matches", 20)),
            )
        elif tool == "risk_scan":
            result = self.tools.risk_scan()
        elif tool == "retrieve_company_policy":
            result = self.tools.retrieve_company_policy(
                str(args.get("query", "")),
                args.get("category"),
                int(args.get("max_results", 3)),
            )
        else:
            return {"type": "tool", "ok": False, "tool": tool, "error": "Tool is not allowed"}
        return {"type": "tool", "ok": True, "tool": tool, "result": self._compact_result(result)}

    def _compact_result(self, result: Any) -> Any:
        if isinstance(result, str):
            return truncate(result, 12000)
        text = json.dumps(result, ensure_ascii=False)
        if len(text) <= 12000:
            return result
        return {"truncated": True, "preview": truncate(text, 12000)}

    def _emit_candidate(self, finding_payload: dict[str, Any], proposed_by: str) -> CouncilFinding:
        finding = Finding(**finding_payload)
        finding.validate()
        item = self.lifecycle.candidate(finding, proposed_by)
        self.evidence.add(item.finding_id, "reviewer_explanation", finding.impact, proposed_by)
        self.evidence.add(item.finding_id, "diff_line", finding.evidence, proposed_by)
        try:
            context = self.tools.read_file_context(finding.file, finding.line, radius=3)
            self.evidence.add(item.finding_id, "file_context", context, "evidence-store")
        except Exception as exc:
            self.evidence.add(item.finding_id, "file_context_error", str(exc), "evidence-store")
        self._attach_company_policy(item.finding_id, finding)
        self.bus.send(
            proposed_by,
            "lead-reviewer",
            "candidate_finding",
            f"{finding.severity} {finding.category}: {finding.title}",
            item.finding_id,
        )
        return item

    def _attach_company_policy(self, finding_id: str, finding: Finding) -> None:
        query = " ".join([finding.title, finding.category, finding.evidence, finding.impact, finding.suggestion])
        policies = self.tools.retrieve_company_policy(query, category=finding.category, max_results=3)
        for policy in policies:
            content = (
                f"{policy['policy_id']} ({policy['doc_path']}#{policy['heading']}): "
                f"{policy['excerpt']} [method={policy['retrieval_method']} score={policy['score']}]"
            )
            self.evidence.add(finding_id, "company_policy", content, "company-rag")

    def _coverage_review(self, pr_description: str) -> None:
        if not self.llm_client or not self.llm_client.enabled:
            return
        try:
            files = self.tools.changed_files()
            diff = self.tools.git_diff()
            risk_signals = self.tools.risk_scan()
        except Exception as exc:
            self.transcript.emit("agent.coverage_review.skipped", reason=str(exc))
            return
        existing = [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()]
        findings = self.llm_client.coverage_review(
            pr_description,
            self.skill_context,
            diff,
            files,
            existing,
            [*self.observations, {"type": "tool", "tool": "risk_scan", "ok": True, "result": risk_signals}],
        )
        added = 0
        skipped = 0
        for finding in findings:
            if self._is_duplicate_finding(finding):
                skipped += 1
                continue
            self._emit_candidate(asdict(finding), proposed_by="agentic-coverage-reviewer")
            added += 1
        promoted = 0
        for signal in risk_signals:
            finding = self._finding_from_risk_signal(signal)
            if not finding or self._is_duplicate_finding(finding):
                continue
            self._emit_candidate(asdict(finding), proposed_by="risk-scan-tool")
            promoted += 1
        self.transcript.emit(
            "agent.coverage_review.complete",
            proposed=len(findings),
            added=added,
            skipped_duplicates=skipped,
            promoted_risk_signals=promoted,
        )

    def _is_duplicate_finding(self, finding: Finding) -> bool:
        normalized_evidence = re.sub(r"\s+", " ", finding.evidence.strip().lower())
        normalized_title = re.sub(r"\s+", " ", finding.title.strip().lower())
        for item in self.lifecycle.all():
            existing = item.finding
            if existing.file != finding.file:
                continue
            existing_evidence = re.sub(r"\s+", " ", existing.evidence.strip().lower())
            existing_title = re.sub(r"\s+", " ", existing.title.strip().lower())
            if existing.line == finding.line and existing.category == finding.category:
                return True
            if normalized_evidence and normalized_evidence == existing_evidence:
                return True
            if existing.line == finding.line and normalized_title == existing_title:
                return True
        return False

    def _finding_from_risk_signal(self, signal: dict[str, Any]) -> Finding | None:
        file = str(signal.get("file", ""))
        line = int(signal.get("line", 1) or 1)
        evidence = str(signal.get("evidence", "")).strip()
        name = str(signal.get("signal", ""))
        templates: dict[str, dict[str, str]] = {
            "sql_interpolation": {
                "severity": "P1",
                "category": "security",
                "title": "SQL query interpolates user-controlled values",
                "impact": "Interpolating request data into SQL can allow injection or unintended data access.",
                "suggestion": "Use the parameter binding API for the actual database driver.",
            },
            "shell_execution": {
                "severity": "P1",
                "category": "security",
                "title": "Shell command uses formatted runtime values",
                "impact": "Using shell=True with formatted values can turn crafted input into command execution.",
                "suggestion": "Avoid shell=True and pass an argument list to subprocess.",
            },
            "mutable_default": {
                "severity": "P2",
                "category": "correctness",
                "title": "Mutable default argument can leak state between calls",
                "impact": "The same object is reused across calls, which can leak state between requests or events.",
                "suggestion": "Default to None and allocate the list or dict inside the function.",
            },
            "swallowed_exception": {
                "severity": "P2",
                "category": "correctness",
                "title": "Exception is swallowed without recovery",
                "impact": "Failures disappear and the function can continue with misleading fallback behavior.",
                "suggestion": "Catch specific exceptions and either handle, log, or re-raise them.",
            },
            "sensitive_logging": {
                "severity": "P1",
                "category": "security",
                "title": "Sensitive values are written to logs",
                "impact": "Tokens, signatures, or card data in logs can be exposed through monitoring or support tooling.",
                "suggestion": "Remove sensitive fields from logs or redact them before logging.",
            },
            "signature_mismatch_not_rejected": {
                "severity": "P1",
                "category": "security",
                "title": "Webhook signature mismatch is not rejected",
                "impact": "Invalidly signed webhook events can continue through the handler and be accepted.",
                "suggestion": "Return an error or raise before processing when signature verification fails.",
            },
            "trust_bypass_pattern": {
                "severity": "P2",
                "category": "correctness",
                "title": "Test-prefix shortcut can trust unintended merchants",
                "impact": "Production merchant IDs matching the test prefix can bypass normal risk checks.",
                "suggestion": "Restrict test shortcuts to non-production environments or explicit fixtures.",
            },
            "risk_rule_approves_high_risk_case": {
                "severity": "P2",
                "category": "correctness",
                "title": "High-risk country branch approves payments",
                "impact": "A risk rule can approve payments from high-risk countries instead of escalating review.",
                "suggestion": "Revisit the business rule and require manual review unless there is documented policy.",
            },
            "negative_amount_refund": {
                "severity": "P2",
                "category": "correctness",
                "title": "Refund handler returns negative amount",
                "impact": "Downstream systems may double-negate or misinterpret negative refund amounts.",
                "suggestion": "Use an explicit event type and normalized positive amount contract.",
            },
        }
        if name.startswith("missing_test_"):
            templates[name] = {
                "severity": "P2",
                "category": "testing",
                "title": "Missing test coverage for critical changed behavior",
                "impact": "Risk-sensitive behavior changed without a targeted regression test.",
                "suggestion": "Add a focused test that exercises this branch and asserts the expected outcome.",
            }
        template = templates.get(name)
        if not template or not file or not evidence:
            return None
        return Finding(
            file=file,
            line=line,
            severity=template["severity"],
            category=template["category"],
            title=template["title"],
            evidence=evidence,
            impact=template["impact"],
            suggestion=template["suggestion"],
        )

    def _ask_critic(self, action: dict[str, Any]) -> dict[str, Any]:
        finding_id = action.get("finding_id", "")
        if not finding_id and isinstance(action.get("finding"), dict):
            finding_id = self._emit_candidate(action["finding"], proposed_by="agentic-reviewer").finding_id
        item = next((candidate for candidate in self.lifecycle.all() if candidate.finding_id == finding_id), None)
        if not item:
            return {"type": "critic", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        if not self.critic_pass:
            return {"type": "critic", "ok": True, "finding_id": finding_id, "result": "critic disabled"}
        self.bus.send(
            "agentic-reviewer",
            "critic-reviewer",
            "task_assignment",
            "Challenge this candidate finding if evidence or severity is weak.",
            finding_id,
        )
        self.critic.review([item])
        return {
            "type": "critic",
            "ok": True,
            "finding_id": finding_id,
            "status": item.status,
            "evidence_chain": self.evidence.list(finding_id),
        }

    def _resolve_candidates(self, default_reason: str) -> None:
        for item in self.lifecycle.all():
            evidence_chain = self.evidence.list(item.finding_id)
            resolution = (
                self.llm_client.resolve_finding(item, evidence_chain, self.skill_context)
                if self.llm_client and self.llm_client.enabled
                else {}
            )
            if resolution:
                self.evidence.add(
                    item.finding_id,
                    "lead_resolution",
                    f"{resolution['resolution']}: {resolution['reason']}",
                    "lead-reviewer",
                )
                if resolution["resolution"] == "rejected":
                    self.lifecycle.reject(item.finding_id, resolution["reason"])
                elif resolution["resolution"] == "downgraded":
                    self.lifecycle.downgrade(item.finding_id, resolution["severity"], resolution["reason"])
                else:
                    self.lifecycle.accept(item.finding_id, resolution["reason"])
            elif item.status == "challenged":
                self.lifecycle.accept(item.finding_id, f"{default_reason} Critic challenge reviewed.")
            else:
                self.lifecycle.accept(item.finding_id, default_reason)
        for item in self.lifecycle.accepted():
            self.collector.emit(item.finding)

    def _run_local_guardrails(self) -> dict[str, Any]:
        self.transcript.emit("agent.action", step=0, action="call_tool", tool="changed_files", args={})
        files = self.tools.changed_files()
        obs = {"type": "tool", "ok": True, "tool": "changed_files", "result": files}
        self.observations.append(obs)
        self.transcript.emit("agent.observation", step=0, **obs)
        self.transcript.emit("agent.action", step=0, action="call_tool", tool="git_diff", args={})
        diff = self.tools.git_diff()
        obs = {"type": "tool", "ok": True, "tool": "git_diff", "result": truncate(diff, 12000)}
        self.observations.append(obs)
        self.transcript.emit("agent.observation", step=0, **obs)
        test_result = self.tools.run_tests(self.test_command) if self.test_command else None
        reviewers = SpecialtyReviewers(self.tools, self.collector, self.transcript)
        for reviewer in (
            "security-reviewer",
            "correctness-reviewer",
            "test-reviewer",
            "maintainability-reviewer",
            "company-policy-reviewer",
        ):
            reviewers.run(reviewer, diff, files, test_result)
        self.transcript.emit("agent.finalize", reason="LLM unavailable; used local guardrails fallback.")
        self.todos.update(
            [
                {"content": "Inspect PR context with read-only tools", "status": "completed"},
                {"content": "Run local guardrail reviewers", "status": "completed"},
                {"content": "Finalize accepted findings and write report", "status": "completed"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)
        self.transcript.emit("agentic.complete", candidates=0, accepted=len(self.collector.findings), fallback=True)
        return {"findings": [], "messages": []}


class ReviewCouncil:
    def __init__(
        self,
        tools: ReviewTools,
        transcript: Transcript,
        collector: FindingCollector,
        critic_pass: bool = True,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
    ):
        self.tools = tools
        self.transcript = transcript
        self.collector = collector
        self.critic_pass = critic_pass
        self.llm_client = llm_client
        self.skill_context = skill_context
        self.bus = MessageBus(transcript)
        self.evidence = EvidenceStore(transcript)
        self.lifecycle = FindingLifecycle(transcript)
        self.members = [
            ReviewAgentMember("security-reviewer", "Security risk reviewer", tools, transcript, llm_client),
            ReviewAgentMember("correctness-reviewer", "Correctness and edge-case reviewer", tools, transcript, llm_client),
            ReviewAgentMember("test-reviewer", "Test coverage reviewer", tools, transcript, llm_client),
            ReviewAgentMember("maintainability-reviewer", "Maintainability reviewer", tools, transcript, llm_client),
            ReviewAgentMember("company-policy-reviewer", "Company policy alignment reviewer", tools, transcript, llm_client),
        ]
        self.critic = CriticReviewer(self.bus, self.evidence, self.lifecycle, llm_client, skill_context)

    def run(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        pr_description: str = "",
    ) -> dict[str, Any]:
        self.transcript.emit("council.start", members=[member.name for member in self.members])
        review_plan = (
            self.llm_client.plan_review(pr_description, self.skill_context, diff, files, test_result)
            if self.llm_client
            else {}
        )
        if review_plan:
            self.transcript.emit("council.plan", assignments=review_plan)
        self.bus.send(
            "lead-reviewer",
            "all",
            "task_assignment",
            "Review the PR diff and submit candidate findings with evidence.",
        )

        for member in self.members:
            focus = review_plan.get(member.name, f"Scope: {member.role}. Submit only actionable findings.")
            self.bus.send(
                "lead-reviewer",
                member.name,
                "task_assignment",
                focus,
            )
            findings = member.review(diff, files, test_result, focus, self.skill_context)
            print(f"[council] {member.name} proposed {len(findings)} candidate finding(s)")
            for finding in findings:
                item = self.lifecycle.candidate(finding, member.name)
                self.evidence.add(item.finding_id, "reviewer_explanation", finding.impact, member.name)
                self.evidence.add(item.finding_id, "diff_line", finding.evidence, member.name)
                try:
                    context = self.tools.read_file_context(finding.file, finding.line, radius=3)
                    self.evidence.add(item.finding_id, "file_context", context, "evidence-store")
                except Exception as exc:
                    self.evidence.add(item.finding_id, "file_context_error", str(exc), "evidence-store")
                self._attach_company_policy(item.finding_id, finding)
                self.bus.send(
                    member.name,
                    "lead-reviewer",
                    "candidate_finding",
                    f"{finding.severity} {finding.category}: {finding.title}",
                    item.finding_id,
                )

        if self.critic_pass:
            self.bus.send(
                "lead-reviewer",
                "critic-reviewer",
                "task_assignment",
                "Challenge weak or merge-blocking findings before final resolution.",
            )
            self.critic.review(self.lifecycle.all())

        self._resolve_findings()
        for item in self.lifecycle.accepted():
            self.collector.emit(item.finding)

        records = [
            item.to_dict(self.evidence.list(item.finding_id))
            for item in self.lifecycle.all()
        ]
        self.transcript.emit("council.complete", candidates=len(records), accepted=len(self.lifecycle.accepted()))
        return {"findings": records, "messages": self.bus.all()}

    def _attach_company_policy(self, finding_id: str, finding: Finding) -> None:
        query = " ".join(
            [
                finding.title,
                finding.category,
                finding.evidence,
                finding.impact,
                finding.suggestion,
            ]
        )
        policies = self.tools.retrieve_company_policy(query, category=finding.category, max_results=3)
        for policy in policies:
            content = (
                f"{policy['policy_id']} ({policy['doc_path']}#{policy['heading']}): "
                f"{policy['excerpt']} [method={policy['retrieval_method']} score={policy['score']}]"
            )
            self.evidence.add(finding_id, "company_policy", content, "company-rag")

    def _resolve_findings(self) -> None:
        for item in self.lifecycle.all():
            evidence_count = len(self.evidence.list(item.finding_id))
            evidence_chain = self.evidence.list(item.finding_id)
            llm_resolution = (
                self.llm_client.resolve_finding(item, evidence_chain, self.skill_context)
                if self.llm_client
                else {}
            )
            if llm_resolution:
                self.evidence.add(
                    item.finding_id,
                    "lead_resolution",
                    f"{llm_resolution['resolution']}: {llm_resolution['reason']}",
                    "lead-reviewer",
                )
                resolution = llm_resolution["resolution"]
                reason = llm_resolution["reason"]
                if item.status == "challenged":
                    defense = (
                        f"Defense: {evidence_count} evidence item(s) include diff evidence and reviewer rationale."
                    )
                    self.evidence.add(item.finding_id, "reviewer_defense", defense, item.proposed_by)
                    self.bus.send(item.proposed_by, "critic-reviewer", "defense", defense, item.finding_id)
                if resolution == "rejected":
                    self.lifecycle.reject(item.finding_id, reason)
                elif resolution == "downgraded":
                    self.lifecycle.downgrade(item.finding_id, llm_resolution["severity"], reason)
                else:
                    self.lifecycle.accept(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)
            elif item.status == "challenged":
                defense = (
                    f"Defense: {evidence_count} evidence item(s) include diff evidence and reviewer rationale."
                )
                self.evidence.add(item.finding_id, "reviewer_defense", defense, item.proposed_by)
                self.bus.send(item.proposed_by, "critic-reviewer", "defense", defense, item.finding_id)
                reason = "Accepted after challenge because evidence chain is sufficient for the report."
                self.lifecycle.accept(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)
            elif evidence_count < 1:
                reason = "Rejected because no evidence was attached."
                self.lifecycle.reject(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)
            else:
                reason = "Accepted by lead reviewer after evidence review."
                self.lifecycle.accept(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)


class DebateCouncilLoop:
    def __init__(
        self,
        tools: ReviewTools,
        transcript: Transcript,
        collector: FindingCollector,
        critic_pass: bool = True,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
        max_actions: int = 12,
        language: str = "zh",
    ):
        self.tools = tools
        self.transcript = transcript
        self.collector = collector
        self.critic_pass = critic_pass
        self.llm_client = llm_client
        self.skill_context = skill_context
        self.max_actions = max_actions
        self.language = language
        self.bus = MessageBus(transcript)
        self.evidence = EvidenceStore(transcript)
        self.lifecycle = FindingLifecycle(transcript)
        self.members = [
            ReviewAgentMember("security-reviewer", "Security risk reviewer", tools, transcript, llm_client),
            ReviewAgentMember("correctness-reviewer", "Correctness and edge-case reviewer", tools, transcript, llm_client),
            ReviewAgentMember("test-reviewer", "Test coverage reviewer", tools, transcript, llm_client),
            ReviewAgentMember("maintainability-reviewer", "Maintainability reviewer", tools, transcript, llm_client),
            ReviewAgentMember("company-policy-reviewer", "Company policy alignment reviewer", tools, transcript, llm_client),
        ]
        self.critic = CriticReviewer(self.bus, self.evidence, self.lifecycle, llm_client, skill_context)
        self.observations: list[dict[str, Any]] = []
        self.report_writer_notes: dict[str, Any] = {}
        self._fallback_critic_ids: set[str] = set()

    def run(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        pr_description: str = "",
    ) -> dict[str, Any]:
        if not self.llm_client or not self.llm_client.enabled:
            self.transcript.emit("debate.fallback", reason="llm unavailable")
            council = ReviewCouncil(
                self.tools,
                self.transcript,
                self.collector,
                self.critic_pass,
                self.llm_client,
                self.skill_context,
            )
            return council.run(diff, files, test_result, pr_description)

        self.transcript.emit(
            "debate.start",
            members=[member.name for member in self.members],
            max_actions=self.max_actions,
        )
        self._seed_candidates(diff, files, test_result, pr_description)
        finalized = False
        for step in range(1, self.max_actions + 1):
            action = self.llm_client.next_debate_action(
                self._debate_state(step, pr_description, files, test_result),
                self.skill_context,
            )
            if not action:
                action = self._fallback_debate_action()
            self.transcript.emit("debate.action", step=step, **action)
            observation = self._apply_debate_action(action)
            self.observations.append(observation)
            self.transcript.emit("debate.observation", step=step, **observation)
            if action["action"] == "finalize":
                finalized = True
                break
        if not finalized:
            self.transcript.emit("debate.finalize", reason="max actions reached")

        self._resolve_remaining()
        for item in self.lifecycle.accepted():
            self.collector.emit(item.finding)

        records = [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()]
        self.transcript.emit("debate.complete", candidates=len(records), accepted=len(self.lifecycle.accepted()))
        return {
            "findings": records,
            "messages": self.bus.all(),
            "report_writer_notes": self.report_writer_notes,
        }

    def _seed_candidates(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        pr_description: str,
    ) -> None:
        review_plan = self.llm_client.plan_review(pr_description, self.skill_context, diff, files, test_result)
        if review_plan:
            self.transcript.emit("debate.plan", assignments=review_plan)
        self.bus.send(
            "lead-reviewer",
            "all",
            "task_assignment",
            "Submit candidate findings with concrete evidence. Debate will challenge weak or duplicate items.",
        )
        for member in self.members:
            focus = review_plan.get(member.name, f"Scope: {member.role}. Submit only actionable findings.")
            self.bus.send("lead-reviewer", member.name, "task_assignment", focus)
            findings = member.review(diff, files, test_result, focus, self.skill_context)
            print(f"[debate] {member.name} proposed {len(findings)} candidate finding(s)")
            for finding in findings:
                item = self.lifecycle.candidate(finding, member.name)
                self.evidence.add(item.finding_id, "reviewer_explanation", finding.impact, member.name)
                self.evidence.add(item.finding_id, "diff_line", finding.evidence, member.name)
                try:
                    context = self.tools.read_file_context(finding.file, finding.line, radius=3)
                    self.evidence.add(item.finding_id, "file_context", context, "evidence-store")
                except Exception as exc:
                    self.evidence.add(item.finding_id, "file_context_error", str(exc), "evidence-store")
                self._attach_company_policy(item.finding_id, finding)
                self.bus.send(
                    member.name,
                    "lead-reviewer",
                    "candidate_finding",
                    f"{finding.severity} {finding.category}: {finding.title}",
                    item.finding_id,
                )

    def _attach_company_policy(self, finding_id: str, finding: Finding) -> None:
        query = " ".join(
            [
                finding.title,
                finding.category,
                finding.evidence,
                finding.impact,
                finding.suggestion,
            ]
        )
        policies = self.tools.retrieve_company_policy(query, category=finding.category, max_results=3)
        for policy in policies:
            content = (
                f"{policy['policy_id']} ({policy['doc_path']}#{policy['heading']}): "
                f"{policy['excerpt']} [method={policy['retrieval_method']} score={policy['score']}]"
            )
            self.evidence.add(finding_id, "company_policy", content, "company-rag")

    def _debate_state(
        self,
        step: int,
        pr_description: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "step": step,
            "max_actions": self.max_actions,
            "pr_description": pr_description[:6000],
            "changed_files": files,
            "test_result": test_result or {},
            "findings": [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()],
            "messages": self.bus.all()[-30:],
            "recent_observations": self.observations[-10:],
            "quality_goal": "maximize true critical coverage while minimizing duplicates, unsupported claims, and severity inflation",
        }

    def _fallback_debate_action(self) -> dict[str, Any]:
        candidate = next(
            (
                item
                for item in self.lifecycle.all()
                if item.status == "candidate" and item.finding_id not in self._fallback_critic_ids
            ),
            None,
        )
        if candidate and self.critic_pass:
            self._fallback_critic_ids.add(candidate.finding_id)
            return {
                "thought": "Fallback asks critic to verify the next unresolved candidate.",
                "action": "ask_critic",
                "finding_id": candidate.finding_id,
                "reason": "LLM debate action was unavailable.",
            }
        unresolved = next((item for item in self.lifecycle.all() if item.status in {"candidate", "challenged"}), None)
        if unresolved:
            return {
                "thought": "Fallback accepts candidate after available evidence review.",
                "action": "accept_finding",
                "finding_id": unresolved.finding_id,
                "reason": "Evidence chain is sufficient for fallback resolution.",
            }
        return {
            "thought": "No unresolved candidates remain.",
            "action": "finalize",
            "reason": "Debate has no unresolved candidates.",
        }

    def _apply_debate_action(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action["action"]
        try:
            if kind == "ask_critic":
                return self._ask_critic(action["finding_id"])
            if kind == "request_reviewer_defense":
                return self._request_reviewer_defense(action["finding_id"], action.get("reason", ""))
            if kind == "request_more_evidence":
                return self._request_more_evidence(action["finding_id"], action.get("reason", ""))
            if kind == "revise_finding":
                finding = Finding(**action["finding"])
                item = self.lifecycle.revise(action["finding_id"], finding, action.get("reason", "Revised by debate controller."))
                self.evidence.add(item.finding_id, "lead_revision", action.get("reason", ""), "lead-reviewer")
                return {"type": "revise", "ok": True, "finding_id": item.finding_id}
            if kind == "merge_duplicates":
                source_id = action["source_id"]
                target_id = action["target_id"]
                reason = action.get("reason", f"Duplicate of {target_id}.")
                self.lifecycle.reject(source_id, reason)
                self.evidence.add(source_id, "duplicate_merge", f"Merged into {target_id}: {reason}", "lead-reviewer")
                self.bus.send("lead-reviewer", "report-writer", "resolution", reason, source_id)
                return {"type": "merge", "ok": True, "source_id": source_id, "target_id": target_id}
            if kind == "accept_finding":
                item = self.lifecycle.accept(action["finding_id"], action.get("reason", "Accepted by debate controller."))
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", item.resolution_reason, item.finding_id)
                return {"type": "resolution", "ok": True, "finding_id": item.finding_id, "status": item.status}
            if kind == "reject_finding":
                item = self.lifecycle.reject(action["finding_id"], action.get("reason", "Rejected by debate controller."))
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", item.resolution_reason, item.finding_id)
                return {"type": "resolution", "ok": True, "finding_id": item.finding_id, "status": item.status}
            if kind == "ask_report_writer":
                context = self._report_context()
                self.report_writer_notes = self.llm_client.report_writer_review(context, self.language) if self.llm_client else {}
                self.transcript.emit("debate.report_writer", chars=len(json.dumps(self.report_writer_notes, ensure_ascii=False)))
                self.bus.send("lead-reviewer", "report-writer", "task_assignment", action.get("reason", "Review final report quality."))
                return {"type": "report_writer", "ok": True, "has_notes": bool(self.report_writer_notes)}
            if kind == "finalize":
                self.transcript.emit("debate.finalize", reason=action.get("reason", "Debate finalized."))
                return {"type": "finalize", "ok": True, "reason": action.get("reason", "")}
        except Exception as exc:
            return {"type": kind, "ok": False, "error": str(exc)}
        return {"type": kind, "ok": False, "error": "Unknown debate action"}

    def _ask_critic(self, finding_id: str) -> dict[str, Any]:
        item = self._find_item(finding_id)
        if not item:
            return {"type": "critic", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        self.bus.send(
            "lead-reviewer",
            "critic-reviewer",
            "task_assignment",
            "Challenge this candidate if evidence, severity, or duplication is weak.",
            finding_id,
        )
        self.critic.review([item])
        return {
            "type": "critic",
            "ok": True,
            "finding_id": finding_id,
            "status": item.status,
            "evidence_chain": self.evidence.list(finding_id),
        }

    def _request_reviewer_defense(self, finding_id: str, reason: str) -> dict[str, Any]:
        item = self._find_item(finding_id)
        if not item:
            return {"type": "defense", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        chain = self.evidence.list(finding_id)
        challenge = reason or item.resolution_reason or "Please defend, revise, or withdraw this finding."
        self.bus.send("lead-reviewer", item.proposed_by, "evidence_request", challenge, finding_id)
        response = self.llm_client.reviewer_defense(item.proposed_by, item, challenge, chain, self.skill_context) if self.llm_client else {}
        if not response:
            defense = f"Defense: {len(chain)} evidence item(s) support this finding."
            self.evidence.add(finding_id, "reviewer_defense", defense, item.proposed_by)
            self.bus.send(item.proposed_by, "lead-reviewer", "defense", defense, finding_id)
            return {"type": "defense", "ok": True, "finding_id": finding_id, "decision": "defend"}
        decision = response["decision"]
        self.evidence.add(finding_id, "reviewer_defense", response["reason"], item.proposed_by)
        self.bus.send(item.proposed_by, "lead-reviewer", "defense", response["reason"], finding_id)
        if decision == "withdraw":
            self.lifecycle.reject(finding_id, response["reason"])
        elif decision == "revise" and isinstance(response.get("finding"), dict):
            self.lifecycle.revise(finding_id, Finding(**response["finding"]), response["reason"])
        return {"type": "defense", "ok": True, "finding_id": finding_id, "decision": decision}

    def _request_more_evidence(self, finding_id: str, reason: str) -> dict[str, Any]:
        item = self._find_item(finding_id)
        if not item:
            return {"type": "evidence", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        try:
            context = self.tools.read_file_context(item.finding.file, item.finding.line, radius=6)
        except Exception as exc:
            self.evidence.add(finding_id, "extra_evidence_error", str(exc), "evidence-store")
            return {"type": "evidence", "ok": False, "finding_id": finding_id, "error": str(exc)}
        self.evidence.add(finding_id, "extra_file_context", context, "evidence-store")
        self.bus.send("lead-reviewer", item.proposed_by, "evidence_request", reason or "Extra evidence was collected.", finding_id)
        return {"type": "evidence", "ok": True, "finding_id": finding_id, "result": truncate(context, 1200)}

    def _resolve_remaining(self) -> None:
        for item in self.lifecycle.all():
            if item.status in {"accepted", "rejected", "downgraded"}:
                continue
            evidence_chain = self.evidence.list(item.finding_id)
            resolution = self.llm_client.resolve_finding(item, evidence_chain, self.skill_context) if self.llm_client else {}
            if resolution:
                self.evidence.add(
                    item.finding_id,
                    "lead_resolution",
                    f"{resolution['resolution']}: {resolution['reason']}",
                    "lead-reviewer",
                )
                if resolution["resolution"] == "rejected":
                    self.lifecycle.reject(item.finding_id, resolution["reason"])
                elif resolution["resolution"] == "downgraded":
                    self.lifecycle.downgrade(item.finding_id, resolution["severity"], resolution["reason"])
                else:
                    self.lifecycle.accept(item.finding_id, resolution["reason"])
            elif item.status == "challenged":
                self.lifecycle.reject(item.finding_id, item.resolution_reason or "Rejected after unresolved challenge.")
            else:
                self.lifecycle.accept(item.finding_id, "Accepted by debate fallback after evidence review.")

    def _report_context(self) -> dict[str, Any]:
        return {
            "findings": [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()],
            "messages": self.bus.all(),
            "observations": self.observations,
            "quality_rules": [
                "Do not reward duplicate findings.",
                "Prefer evidence-backed severe issues.",
                "Keep report style standardized for judge fairness.",
            ],
        }

    def _find_item(self, finding_id: str) -> CouncilFinding | None:
        return next((item for item in self.lifecycle.all() if item.finding_id == finding_id), None)
