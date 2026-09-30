"""Modularized llm component of the PR review agent."""

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

from .domain import CouncilFinding, Finding, FindingCollector, Transcript
from .settings import MAX_DIFF_CHARS, MAX_SKILL_CHARS, SEVERITIES
from .utils import truncate


class LLMClient:
    provider = "none"
    enabled = False

    def plan_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> dict[str, str]:
        return {}

    def review(
        self,
        reviewer_name: str,
        reviewer_role: str,
        focus: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> list[Finding]:
        return []

    def critique_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        return {}

    def resolve_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        return {}

    def next_action(
        self,
        agent_state: dict[str, Any],
        skill_context: str,
        tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {}

    def coverage_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        existing_findings: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> list[Finding]:
        return []

    def next_debate_action(
        self,
        debate_state: dict[str, Any],
        skill_context: str,
    ) -> dict[str, Any]:
        return {}

    def reviewer_defense(
        self,
        reviewer_name: str,
        item: CouncilFinding,
        challenge: str,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, Any]:
        return {}

    def report_writer_review(
        self,
        report_context: dict[str, Any],
        language: str = "zh",
    ) -> dict[str, Any]:
        return {}

    def judge_report(
        self,
        judge_input: dict[str, Any],
        diff: str,
        pr_description: str,
    ) -> dict[str, Any]:
        return {}


class OpenAICompatibleClient(LLMClient):

    def __init__(
        self,
        api_key: str | None,
        model: str,
        base_url: str,
        transcript: Transcript,
        timeout: int = 60,
        provider: str = "aliyun",
    ):
        self.provider = provider
        self.api_key = api_key or ""
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.transcript = transcript
        self.timeout = timeout
        self.enabled = bool(self.api_key)
        self._pending_actions: list[dict[str, Any]] = []
        self._last_extra_json_payloads: list[Any] = []

    def _chat_json(self, agent_name: str, system_prompt: str, user_prompt: str) -> Any:
        self._last_extra_json_payloads = []
        if not self.enabled:
            self.transcript.emit(
                "llm.skipped",
                provider=self.provider,
                reviewer=agent_name,
                reason=f"{self.provider.upper()} API key is not set",
            )
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }
        endpoint = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        self.transcript.emit(
            "llm.request",
            provider=self.provider,
            reviewer=agent_name,
            model=self.model,
            endpoint=endpoint,
            prompt_chars=len(user_prompt),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.transcript.emit(
                "llm.error",
                provider=self.provider,
                reviewer=agent_name,
                error=str(exc),
            )
            return None

        self.transcript.emit(
            "llm.response",
            provider=self.provider,
            reviewer=agent_name,
            chars=len(raw),
        )
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            return self._parse_json_content(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            self.transcript.emit(
                "llm.parse_error",
                provider=self.provider,
                reviewer=agent_name,
                error=str(exc),
                raw=truncate(raw, 2000),
            )
            return None

    def plan_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> dict[str, str]:
        schema = {
            "assignments": [
                {
                    "reviewer": "security-reviewer",
                    "focus": "specific review focus for this PR",
                }
            ]
        }
        prompt = "\n".join(
            [
                "Create a concise review plan for a multi-agent PR review council.",
                "Allowed reviewers: security-reviewer, correctness-reviewer, test-reviewer, maintainability-reviewer, company-policy-reviewer.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "PR description:",
                pr_description[:8000],
                "Changed files:",
                json.dumps(files, ensure_ascii=False),
                "Test result:",
                json.dumps(test_result or {}, ensure_ascii=False)[:4000],
                "Diff:",
                truncate(diff, 40000),
            ]
        )
        payload = self._chat_json("lead-reviewer.plan", self._system_prompt("lead-reviewer.plan"), prompt)
        if not isinstance(payload, dict):
            return {}
        assignments = payload.get("assignments", [])
        if not isinstance(assignments, list):
            return {}
        allowed = {
            "security-reviewer",
            "correctness-reviewer",
            "test-reviewer",
            "maintainability-reviewer",
            "company-policy-reviewer",
        }
        plan: dict[str, str] = {}
        for item in assignments:
            if not isinstance(item, dict):
                continue
            reviewer = str(item.get("reviewer", "")).strip()
            focus = str(item.get("focus", "")).strip()
            if reviewer in allowed and focus:
                plan[reviewer] = focus[:1000]
        return plan

    def review(
        self,
        reviewer_name: str,
        reviewer_role: str,
        focus: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> list[Finding]:
        prompt = self._prompt(reviewer_name, reviewer_role, focus, skill_context, diff, files, test_result)
        payload = self._chat_json(reviewer_name, self._system_prompt(reviewer_name), prompt)
        if payload is None:
            return []
        return self._parse_findings_payload(payload)

    def critique_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        schema = {
            "decision": "challenge",
            "reason": "specific reason",
        }
        prompt = "\n".join(
            [
                "Review this proposed finding. Do not find new issues.",
                "Decide whether the evidence is sufficient and the severity is justified.",
                "Use decision 'challenge' or 'no_challenge'. Return JSON only:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Finding:",
                json.dumps(item.to_dict(evidence_chain), ensure_ascii=False)[:12000],
            ]
        )
        payload = self._chat_json("critic-reviewer", self._system_prompt("critic-reviewer"), prompt)
        if not isinstance(payload, dict):
            return {}
        decision = str(payload.get("decision", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if decision not in {"challenge", "no_challenge"} or not reason:
            return {}
        return {"decision": decision, "reason": reason[:1200]}

    def resolve_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        schema = {
            "resolution": "accepted",
            "reason": "specific reason",
            "severity": "P2",
        }
        prompt = "\n".join(
            [
                "Make the final lead-reviewer resolution for this finding.",
                "Use resolution accepted, rejected, or downgraded.",
                "If downgraded, include the new severity P0/P1/P2/P3.",
                "Prefer evidence-backed findings over volume. Return JSON only:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Finding:",
                json.dumps(item.to_dict(evidence_chain), ensure_ascii=False)[:16000],
            ]
        )
        payload = self._chat_json("lead-reviewer.resolve", self._system_prompt("lead-reviewer.resolve"), prompt)
        if not isinstance(payload, dict):
            return {}
        resolution = str(payload.get("resolution", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        severity = str(payload.get("severity", item.finding.severity)).strip()
        if resolution not in {"accepted", "rejected", "downgraded"} or not reason:
            return {}
        if severity not in SEVERITIES:
            severity = item.finding.severity
        return {"resolution": resolution, "reason": reason[:1200], "severity": severity}

    def coverage_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        existing_findings: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> list[Finding]:
        schema = {
            "findings": [
                {
                    "file": "path/to/file.py",
                    "line": 42,
                    "severity": "P1",
                    "category": "security",
                    "title": "short actionable title",
                    "evidence": "specific diff line or code snippet",
                    "impact": "why this matters",
                    "suggestion": "concrete fix",
                }
            ]
        }
        prompt = "\n".join(
            [
                "You are the same autonomous PR review agent doing a final coverage reflection before finalize.",
                "This is not a role-based workflow. Do one holistic pass over the full diff and current evidence.",
                "Find high-confidence issues the dynamic loop may have missed.",
                "The recent observations may include a risk_scan result. Treat those risk signals as candidate evidence,",
                "and convert every high-confidence non-duplicate risk signal into a finding.",
                "Prioritize merge-blocking or reviewer-worthy findings across:",
                "- injection and unsafe command execution",
                "- secrets, sensitive logging, and trust-boundary mistakes",
                "- authentication, webhook signature, idempotency, and replay handling",
                "- mutable default arguments or shared state across requests",
                "- swallowed exceptions and fail-open behavior",
                "- suspicious business logic bypasses",
                "- missing tests for critical changed behavior",
                "If risk_scan reports testing signals, prefer concrete missing-test findings tied to the exact changed behavior.",
                "Avoid duplicates of existing findings. Do not invent issues outside the diff.",
                "Do not report documented placeholders such as your_api_key in .env.example as leaked real secrets.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "PR description:",
                pr_description[:8000],
                "Changed files:",
                json.dumps(files, ensure_ascii=False),
                "Existing findings:",
                json.dumps(existing_findings, ensure_ascii=False)[:12000],
                "Recent observations:",
                json.dumps(observations[-12:], ensure_ascii=False)[:16000],
                "Full diff:",
                truncate(diff, MAX_DIFF_CHARS),
            ]
        )
        payload = self._chat_json(
            "agentic-reviewer.coverage_review",
            self._system_prompt("agentic-reviewer"),
            prompt,
        )
        if payload is None:
            return []
        return self._parse_findings_payload(payload)

    def next_debate_action(
        self,
        debate_state: dict[str, Any],
        skill_context: str,
    ) -> dict[str, Any]:
        schema = {
            "thought": "short reason for the next debate step",
            "action": "ask_critic",
            "finding_id": "F-001",
            "reason": "why this action is useful",
        }
        allowed = [
            "ask_critic",
            "request_reviewer_defense",
            "request_more_evidence",
            "revise_finding",
            "merge_duplicates",
            "accept_finding",
            "reject_finding",
            "ask_report_writer",
            "finalize",
        ]
        prompt = "\n".join(
            [
                "You are the lead controller of a multi-agent code review debate council.",
                "Do not follow a fixed workflow. Choose the single most useful next debate action.",
                "The goal is high-quality review, not a larger number of findings.",
                "Prefer actions that reduce duplicates, improve evidence, correct severity, or remove noise.",
                "Allowed actions:",
                ", ".join(allowed),
                "For revise_finding include finding_id, finding, and reason.",
                "For merge_duplicates include source_id, target_id, and reason.",
                "For accept_finding/reject_finding include finding_id and reason.",
                "For request_more_evidence include finding_id and reason.",
                "For ask_report_writer include reason.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Current debate state:",
                json.dumps(debate_state, ensure_ascii=False)[:40000],
            ]
        )
        payload = self._chat_json("lead-reviewer.debate", self._system_prompt("lead-reviewer.debate"), prompt)
        if not isinstance(payload, dict):
            return {}
        return self._normalize_debate_action(payload)

    def reviewer_defense(
        self,
        reviewer_name: str,
        item: CouncilFinding,
        challenge: str,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, Any]:
        schema = {
            "decision": "defend",
            "reason": "why the finding should stand, be revised, or be withdrawn",
            "finding": {
                "file": "path/to/file.py",
                "line": 42,
                "severity": "P2",
                "category": "security",
                "title": "revised title if decision is revise",
                "evidence": "specific evidence",
                "impact": "impact",
                "suggestion": "fix",
            },
        }
        prompt = "\n".join(
            [
                f"You are {reviewer_name} responding to a critic challenge.",
                "Choose decision defend, revise, or withdraw.",
                "If revise, include a complete revised finding. If defend or withdraw, finding may be omitted.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Challenge:",
                challenge[:4000],
                "Finding and evidence:",
                json.dumps(item.to_dict(evidence_chain), ensure_ascii=False)[:16000],
            ]
        )
        payload = self._chat_json(reviewer_name + ".defense", self._system_prompt(reviewer_name), prompt)
        if not isinstance(payload, dict):
            return {}
        decision = str(payload.get("decision", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if decision not in {"defend", "revise", "withdraw"} or not reason:
            return {}
        result: dict[str, Any] = {"decision": decision, "reason": reason[:1200]}
        finding = payload.get("finding")
        if decision == "revise" and isinstance(finding, dict):
            try:
                Finding(**finding).validate()
            except (TypeError, ValueError):
                return {}
            result["finding"] = finding
        return result

    def report_writer_review(
        self,
        report_context: dict[str, Any],
        language: str = "zh",
    ) -> dict[str, Any]:
        schema = {
            "verdict": "request_changes",
            "summary_points": ["short factual point"],
            "accepted_findings": [
                {
                    "issue": "short issue",
                    "severity": "P1",
                    "file": "path/to/file.py",
                    "line": 42,
                    "evidence": "specific evidence",
                    "impact": "impact",
                    "fix": "fix",
                    "why_accepted": "evidence-backed reason",
                    "critic_notes": "critic or lead notes",
                    "policy_references": ["company policy id or source"],
                }
            ],
            "rejected_findings": [],
            "downgraded_findings": [],
            "duplicate_notes": [],
            "critic_notes": [],
        }
        prompt = "\n".join(
            [
                "You are ReportWriterAgent for a PR review council.",
                "Standardize the report into fixed JSON fields. Do not write free-form Markdown.",
                "Use short factual sentences. Avoid rhetorical style, marketing language, or literary polish.",
                "The downstream AI judge should score evidence quality, not writing style.",
                f"Report language: {language}",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Report context:",
                json.dumps(report_context, ensure_ascii=False)[:50000],
            ]
        )
        payload = self._chat_json("report-writer", self._system_prompt("report-writer"), prompt)
        return payload if isinstance(payload, dict) else {}

    def judge_report(
        self,
        judge_input: dict[str, Any],
        diff: str,
        pr_description: str,
    ) -> dict[str, Any]:
        schema = {
            "overall_score": 85,
            "verdict": "pass",
            "dimensions": {
                "critical_issue_coverage": 0,
                "evidence_quality": 0,
                "severity_accuracy": 0,
                "duplicate_noise_control": 0,
                "actionability": 0,
                "report_clarity": 0,
            },
            "strengths": ["short point"],
            "weaknesses": ["short point"],
            "recommendations": ["short point"],
        }
        prompt = "\n".join(
            [
                "You are an independent AI judge for PR review quality.",
                "Ignore writing polish and rhetorical style. Score only review quality and evidence quality.",
                "Penalize duplicate findings, unsupported claims, severity inflation, and missed critical issues.",
                "Score each dimension from 0 to 100. Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "PR description:",
                pr_description[:8000],
                "Standardized judge input:",
                json.dumps(judge_input, ensure_ascii=False)[:50000],
                "Diff:",
                truncate(diff, MAX_DIFF_CHARS),
            ]
        )
        payload = self._chat_json("ai-judge", self._system_prompt("ai-judge"), prompt)
        return payload if isinstance(payload, dict) else {}

    def next_action(
        self,
        agent_state: dict[str, Any],
        skill_context: str,
        tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tool_names = {tool["name"] for tool in tools}
        while self._pending_actions:
            queued = self._pending_actions.pop(0)
            normalized = self._normalize_action(queued, tool_names)
            if normalized:
                self.transcript.emit(
                    "llm.queued_action",
                    provider=self.provider,
                    reviewer="agentic-reviewer.next_action",
                    action=normalized["action"],
                    remaining=len(self._pending_actions),
                )
                return normalized

        schema = {
            "thought": "short private planning summary",
            "action": "call_tool",
            "tool": "changed_files",
            "args": {},
        }
        prompt = "\n".join(
            [
                "You are running an autonomous read-only PR review loop.",
                "Choose exactly one next action. Do not follow a fixed workflow if another action is more useful.",
                "Allowed actions:",
                "- update_todos: include todos=[{content,status}] with status pending/in_progress/completed",
                "- call_tool: include tool and args",
                "- emit_finding: include finding with file,line,severity,category,title,evidence,impact,suggestion",
                "- ask_critic: include finding_id for an existing candidate finding",
                "- finalize: include reason when enough investigation has been done",
                "You may return either one JSON action or a JSON array of independent next actions.",
                "When returning a batch, do not include finalize; the runtime will execute queued actions after each observation.",
                "Do not report placeholder examples such as your_api_key in .env.example as leaked real secrets.",
                "Return JSON only, using this object shape for each action:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Available tools:",
                json.dumps(tools, ensure_ascii=False)[:12000],
                "Current agent state:",
                json.dumps(agent_state, ensure_ascii=False)[:16000],
                "Recent observations:",
                json.dumps(observations[-8:], ensure_ascii=False)[:16000],
            ]
        )
        payload = self._chat_json("agentic-reviewer.next_action", self._system_prompt("agentic-reviewer"), prompt)
        if isinstance(payload, list):
            actions = [item for item in payload if isinstance(item, dict)]
            if not actions:
                return {}
            payload = actions[0]
            self._last_extra_json_payloads.extend(actions[1:])
        if not isinstance(payload, dict):
            return {}
        self._enqueue_extra_actions(tool_names)
        normalized = self._normalize_action(payload, tool_names)
        if normalized:
            return normalized
        self.transcript.emit(
            "llm.invalid_action",
            provider=self.provider,
            reviewer="agentic-reviewer.next_action",
            raw=truncate(json.dumps(payload, ensure_ascii=False), 2000),
        )
        repair_prompt = "\n".join(
            [
                "The previous response was not a valid action for the PR review agent.",
                "Rewrite it as exactly one valid JSON action with no commentary.",
                "Allowed actions: update_todos, call_tool, emit_finding, ask_critic, finalize.",
                "For call_tool use fields: action, tool, args.",
                "For finalize use fields: action, reason.",
                "Available tool names:",
                json.dumps(sorted(tool_names), ensure_ascii=False),
                "Invalid response:",
                json.dumps(payload, ensure_ascii=False)[:4000],
            ]
        )
        repaired = self._chat_json(
            "agentic-reviewer.repair_action",
            self._system_prompt("agentic-reviewer"),
            repair_prompt,
        )
        if not isinstance(repaired, dict):
            return {}
        normalized = self._normalize_action(repaired, tool_names)
        if not normalized:
            self.transcript.emit(
                "llm.invalid_action",
                provider=self.provider,
                reviewer="agentic-reviewer.repair_action",
                raw=truncate(json.dumps(repaired, ensure_ascii=False), 2000),
            )
        return normalized

    def _enqueue_extra_actions(self, tool_names: set[str]) -> None:
        queued: list[dict[str, Any]] = []
        for payload in self._last_extra_json_payloads:
            if not isinstance(payload, dict):
                continue
            normalized = self._normalize_action(payload, tool_names)
            if normalized and normalized["action"] != "finalize":
                queued.append(payload)
        if not queued:
            return
        self._pending_actions.extend(queued)
        self.transcript.emit(
            "llm.action_queue.extend",
            provider=self.provider,
            reviewer="agentic-reviewer.next_action",
            count=len(queued),
            pending=len(self._pending_actions),
        )

    def _system_prompt(self, agent_name: str) -> str:
        base = "Return only valid JSON. Do not include markdown fences or commentary."
        prompts = {
            "lead-reviewer.plan": (
                "You are the lead reviewer coordinating a multi-agent PR review council. "
                "Plan focused assignments from PR context, changed files, tests, and diff. "
                "Prefer precise, risk-driven review focus over broad generic instructions. "
                + base
            ),
            "lead-reviewer.resolve": (
                "You are the lead reviewer making final resolutions after specialist review and critic review. "
                "Accept only findings supported by concrete evidence from the diff or source context. "
                "Reject weak or unrelated findings; downgrade overstated severity. "
                "Reject findings that treat documented placeholders such as your_api_key in .env.example as leaked real secrets. "
                + base
            ),
            "lead-reviewer.debate": (
                "You are a debate controller for a multi-agent PR review council. "
                "Your job is to improve review quality through targeted challenge, defense, evidence gathering, "
                "deduplication, and final resolution. Optimize for true critical coverage, low noise, and clear evidence. "
                + base
            ),
            "security-reviewer": (
                "You are a senior security code review agent. Focus only on exploitable security risks "
                "introduced by this diff: secrets, auth bypass, injection, unsafe command execution, "
                "sensitive logging, crypto, and trust-boundary mistakes. Every finding must cite concrete evidence. "
                + base
            ),
            "correctness-reviewer": (
                "You are a senior correctness reviewer. Focus on business logic regressions, edge cases, "
                "exception handling, state bugs, idempotency, data consistency, and payment correctness. "
                "Avoid security-only or style-only issues unless they cause correctness failures. "
                + base
            ),
            "test-reviewer": (
                "You are a senior test reviewer. Focus on missing tests for changed behavior, edge cases, "
                "failure paths, security-sensitive paths, and whether the configured tests are meaningful. "
                "Do not report implementation issues unless the key problem is missing test coverage. "
                + base
            ),
            "maintainability-reviewer": (
                "You are a senior maintainability reviewer. Focus on long-term code health: complexity, "
                "coupling, confusing control flow, unsafe shared state, observability, and operational clarity. "
                "Avoid duplicating security or correctness findings unless maintainability is the main issue. "
                + base
            ),
            "company-policy-reviewer": (
                "You are a company policy alignment reviewer. Focus only on violations of the provided company "
                "knowledge, security baselines, payment policies, testing standards, and incident cases. "
                "Every finding must cite concrete code evidence and a company policy or incident reference. "
                "Do not report generic issues unless the company policy context makes them actionable. "
                + base
            ),
            "critic-reviewer": (
                "You are a skeptical review critic. Your job is not to find new issues, but to verify whether "
                "a proposed finding is well-supported, scoped to the diff, correctly categorized, and assigned "
                "an appropriate severity. Challenge weak evidence, vague impact, wrong severity, or non-actionable findings. "
                "Challenge findings that confuse example placeholders with real committed credentials. "
                + base
            ),
            "agentic-reviewer": (
                "You are an autonomous read-only PR review agent inspired by Claude Code and Codex. "
                "Use an observe-think-act loop: update todos, call tools, inspect observations, gather evidence, "
                "ask the critic for risky findings, and finalize only when you have enough evidence. "
                "Never modify files. Prefer targeted tool calls over fixed reviewer pipelines. "
                + base
            ),
            "report-writer": (
                "You are a structured report writer agent. Produce only standardized JSON fields. "
                "Use plain factual language and avoid style flourishes so evaluation scores reflect review quality. "
                + base
            ),
            "ai-judge": (
                "You are an independent evaluator of PR review reports. Ignore writing polish and score evidence, "
                "coverage of critical issues, severity accuracy, duplicate/noise control, and actionability. "
                + base
            ),
        }
        return prompts.get(agent_name, "You are a senior code review agent. " + base)

    def _prompt(
        self,
        reviewer_name: str,
        reviewer_role: str,
        focus: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> str:
        schema = {
            "findings": [
                {
                    "file": "path/to/file.py",
                    "line": 42,
                    "severity": "P1",
                    "category": "security",
                    "title": "short actionable title",
                    "evidence": "specific diff line or code snippet",
                    "impact": "why this matters",
                    "suggestion": "concrete fix",
                }
            ]
        }
        return "\n".join(
            [
                f"Reviewer: {reviewer_name}",
                f"Role: {reviewer_role}",
                "Lead reviewer focus for this PR:",
                focus or "Use the role scope and the concrete diff evidence.",
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Review only this role's scope. Prefer high-confidence, actionable issues.",
                "Use severity P0/P1/P2/P3 and category security/correctness/performance/testing/maintainability.",
                "Return JSON exactly in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Changed files:",
                json.dumps(files, ensure_ascii=False),
                "Test result:",
                json.dumps(test_result or {}, ensure_ascii=False)[:4000],
                "Diff:",
                truncate(diff, MAX_DIFF_CHARS),
            ]
        )

    def _parse_json_content(self, content: str) -> Any:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            objects = self._parse_sequential_json_objects(cleaned)
            if objects:
                self._last_extra_json_payloads = objects[1:]
                if self._last_extra_json_payloads:
                    self.transcript.emit(
                        "llm.extra_json_queued",
                        count=len(self._last_extra_json_payloads),
                    )
                return objects[0]

            match = re.search(r"\{.*\}", cleaned, flags=re.S)
            if not match:
                raise
            matched = match.group(0)
            try:
                return json.loads(matched)
            except json.JSONDecodeError:
                objects = self._parse_sequential_json_objects(matched)
                if not objects:
                    raise
                self._last_extra_json_payloads = objects[1:]
                if self._last_extra_json_payloads:
                    self.transcript.emit(
                        "llm.extra_json_queued",
                        count=len(self._last_extra_json_payloads),
                    )
                return objects[0]

    def _parse_sequential_json_objects(self, text: str) -> list[Any]:
        decoder = json.JSONDecoder()
        objects: list[Any] = []
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            try:
                value, end_index = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                if objects:
                    self.transcript.emit(
                        "llm.trailing_text_ignored",
                        ignored_chars=len(text) - index,
                        preview=truncate(text[index:], 500),
                    )
                break
            objects.append(value)
            index = end_index
        return objects

    def _parse_findings_payload(self, payload: Any) -> list[Finding]:
        if isinstance(payload, dict):
            items = payload.get("findings", [])
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        collector = FindingCollector()
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                collector.emit(item)
            except ValueError:
                continue
        return collector.sorted()

    def _normalize_action(self, payload: dict[str, Any], tool_names: set[str]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        aliases = {
            "tool_call": "call_tool",
            "use_tool": "call_tool",
            "create_finding": "emit_finding",
            "report_finding": "emit_finding",
            "critic": "ask_critic",
            "finish": "finalize",
            "done": "finalize",
            "final": "finalize",
        }
        action = aliases.get(action, action)
        if action in tool_names:
            args = payload.get("args", payload.get("parameters"))
            if not isinstance(args, dict):
                args = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"thought", "action", "tool", "tool_name"}
                }
            payload = {**payload, "action": "call_tool", "tool": action, "args": args}
            action = "call_tool"
        if action not in {"update_todos", "call_tool", "emit_finding", "ask_critic", "finalize"}:
            return {}
        normalized: dict[str, Any] = {
            "thought": str(payload.get("thought", "")).strip()[:1200],
            "action": action,
        }
        if action == "update_todos":
            todos = payload.get("todos")
            if not isinstance(todos, list):
                return {}
            normalized["todos"] = todos
        elif action == "call_tool":
            tool = str(payload.get("tool") or payload.get("tool_name") or "").strip()
            if tool not in tool_names:
                return {}
            args = payload.get("args", payload.get("parameters", {}))
            normalized["tool"] = tool
            normalized["args"] = args if isinstance(args, dict) else {}
        elif action == "emit_finding":
            finding = payload.get("finding")
            if not isinstance(finding, dict):
                return {}
            try:
                Finding(**finding).validate()
            except (TypeError, ValueError):
                return {}
            normalized["finding"] = finding
        elif action == "ask_critic":
            finding_id = str(payload.get("finding_id", "")).strip()
            finding = payload.get("finding")
            if finding_id:
                normalized["finding_id"] = finding_id
            elif isinstance(finding, dict):
                normalized["finding"] = finding
            else:
                return {}
        elif action == "finalize":
            normalized["reason"] = str(payload.get("reason", "")).strip()[:1200] or "Agent decided review is complete."
        return normalized


    def _normalize_debate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        aliases = {
            "critic": "ask_critic",
            "challenge": "ask_critic",
            "defense": "request_reviewer_defense",
            "more_evidence": "request_more_evidence",
            "revise": "revise_finding",
            "merge": "merge_duplicates",
            "accept": "accept_finding",
            "reject": "reject_finding",
            "report_writer": "ask_report_writer",
            "finish": "finalize",
            "done": "finalize",
        }
        action = aliases.get(action, action)
        allowed = {
            "ask_critic",
            "request_reviewer_defense",
            "request_more_evidence",
            "revise_finding",
            "merge_duplicates",
            "accept_finding",
            "reject_finding",
            "ask_report_writer",
            "finalize",
        }
        if action not in allowed:
            return {}
        normalized: dict[str, Any] = {
            "thought": str(payload.get("thought", "")).strip()[:1200],
            "action": action,
            "reason": str(payload.get("reason", "")).strip()[:1200],
        }
        if action in {
            "ask_critic",
            "request_reviewer_defense",
            "request_more_evidence",
            "accept_finding",
            "reject_finding",
        }:
            finding_id = str(payload.get("finding_id", "")).strip()
            if not finding_id:
                return {}
            normalized["finding_id"] = finding_id
        elif action == "revise_finding":
            finding_id = str(payload.get("finding_id", "")).strip()
            finding = payload.get("finding")
            if not finding_id or not isinstance(finding, dict):
                return {}
            try:
                Finding(**finding).validate()
            except (TypeError, ValueError):
                return {}
            normalized["finding_id"] = finding_id
            normalized["finding"] = finding
        elif action == "merge_duplicates":
            source_id = str(payload.get("source_id") or payload.get("duplicate_id") or "").strip()
            target_id = str(payload.get("target_id") or payload.get("canonical_id") or "").strip()
            if not source_id or not target_id or source_id == target_id:
                return {}
            normalized["source_id"] = source_id
            normalized["target_id"] = target_id
        elif action == "finalize":
            normalized["reason"] = normalized["reason"] or "Debate controller decided review is complete."
        return normalized


AliyunDashScopeClient = OpenAICompatibleClient
