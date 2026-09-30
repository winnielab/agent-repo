"""Modularized reporting component of the PR review agent."""

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

from .domain import FindingCollector, Transcript
from .llm import LLMClient
from .settings import (FINDINGS_NAME, JUDGE_INPUT_NAME, REPORT_NAME,
    SEVERITIES, VERDICTS)


ZH_CATEGORY = {
    "security": "安全",
    "correctness": "正确性",
    "performance": "性能",
    "testing": "测试",
    "maintainability": "可维护性",
}


ZH_TITLE = {
    "Likely secret committed in code": "疑似密钥被提交到代码中",
    "Shell command is built on an unsafe execution surface": "Shell 命令执行面存在风险",
    "SQL query appears to interpolate values directly": "SQL 查询疑似直接拼接参数",
    "Mutable default argument can leak state between calls": "可变默认参数可能导致跨调用状态泄漏",
    "Exception is swallowed without handling": "异常被吞掉且没有处理",
    "None comparison should use identity checks": "None 比较应使用身份判断",
    "Production code changed without nearby test changes": "生产代码变更缺少对应测试",
    "Configured test command failed": "配置的测试命令执行失败",
}


ZH_TEXT = {
    "Credentials in source control can be copied from every clone and CI log.": "\u51ed\u8bc1\u8fdb\u5165\u4ee3\u7801\u4ed3\u5e93\u540e\uff0c\u4f1a\u88ab\u6bcf\u4e2a clone\u3001\u526f\u672c\u548c CI \u65e5\u5fd7\u7ee7\u7eed\u4f20\u64ad\u3002",
    "Move the value to a secret manager or environment variable and rotate the leaked credential.": "\u5c06\u8be5\u503c\u79fb\u5230\u5bc6\u94a5\u7ba1\u7406\u7cfb\u7edf\u6216\u73af\u5883\u53d8\u91cf\uff0c\u5e76\u8f6e\u6362\u5df2\u7ecf\u6cc4\u9732\u7684\u51ed\u8bc1\u3002",
    "User-controlled input can become command execution if it reaches this call.": "\u5982\u679c\u7528\u6237\u53ef\u63a7\u8f93\u5165\u6d41\u5165\u8fd9\u91cc\uff0c\u53ef\u80fd\u6f14\u53d8\u4e3a\u547d\u4ee4\u6267\u884c\u98ce\u9669\u3002",
    "Use subprocess with an argument list, validate inputs, and keep shell=False.": "\u4f7f\u7528\u53c2\u6570\u5217\u8868\u5f62\u5f0f\u8c03\u7528 subprocess\uff0c\u6821\u9a8c\u8f93\u5165\uff0c\u5e76\u4fdd\u6301 shell=False\u3002",
    "Interpolated SQL can allow injection and data exposure.": "\u76f4\u63a5\u62fc\u63a5 SQL \u53ef\u80fd\u5bfc\u81f4\u6ce8\u5165\u548c\u6570\u636e\u6cc4\u9732\u3002",
    "Use parameterized queries provided by the database driver.": "\u4f7f\u7528\u6570\u636e\u5e93\u9a71\u52a8\u63d0\u4f9b\u7684\u53c2\u6570\u5316\u67e5\u8be2\u3002",
    "Large mixed changes are harder to review and increase regression risk.": "\u8fc7\u5927\u7684\u6df7\u5408\u53d8\u66f4\u4f1a\u63d0\u9ad8\u5ba1\u67e5\u96be\u5ea6\u548c\u56de\u5f52\u98ce\u9669\u3002",
    "Split unrelated changes or add a short design note explaining the review strategy.": "\u62c6\u5206\u65e0\u5173\u53d8\u66f4\uff0c\u6216\u8865\u5145\u7b80\u77ed\u8bbe\u8ba1\u8bf4\u660e\u89e3\u91ca\u5ba1\u67e5\u7b56\u7565\u3002",
    "Markers like TODO/FIXME can hide incomplete behavior after merge.": "TODO/FIXME \u7b49\u6807\u8bb0\u53ef\u80fd\u8ba9\u672a\u5b8c\u6210\u903b\u8f91\u968f PR \u5408\u5165\u3002",
    "Resolve the marker before merge or link it to a tracked follow-up task.": "\u5408\u5165\u524d\u89e3\u51b3\u8be5\u6807\u8bb0\uff0c\u6216\u5173\u8054\u5230\u53ef\u8ffd\u8e2a\u7684\u540e\u7eed\u4efb\u52a1\u3002",
    "Failures disappear, making data loss and partial writes hard to detect.": "\u5931\u8d25\u4f1a\u88ab\u9759\u9ed8\u541e\u6389\uff0c\u6570\u636e\u4e22\u5931\u6216\u90e8\u5206\u5199\u5165\u5c06\u66f4\u96be\u53d1\u73b0\u3002",
    "Handle the specific exception, log enough context, or re-raise after cleanup.": "\u5904\u7406\u5177\u4f53\u5f02\u5e38\uff0c\u8bb0\u5f55\u8db3\u591f\u4e0a\u4e0b\u6587\uff0c\u6216\u5728\u6e05\u7406\u540e\u91cd\u65b0\u629b\u51fa\u3002",
    "The same list or dict instance is reused across calls, causing surprising cross-request state.": "\u540c\u4e00\u4e2a list \u6216 dict \u5b9e\u4f8b\u4f1a\u5728\u591a\u6b21\u8c03\u7528\u95f4\u590d\u7528\uff0c\u5bb9\u6613\u9020\u6210\u8de8\u8bf7\u6c42\u72b6\u6001\u6c61\u67d3\u3002",
    "Default to None and create the mutable object inside the function.": "\u9ed8\u8ba4\u503c\u6539\u4e3a None\uff0c\u5e76\u5728\u51fd\u6570\u5185\u90e8\u521b\u5efa\u53ef\u53d8\u5bf9\u8c61\u3002",
    "Equality operators can be overloaded and make null checks less predictable.": "\u7b49\u53f7\u8fd0\u7b97\u7b26\u53ef\u80fd\u88ab\u91cd\u8f7d\uff0c\u8ba9\u7a7a\u503c\u5224\u65ad\u53d8\u5f97\u4e0d\u53ef\u9884\u6d4b\u3002",
    "Use 'is None' or 'is not None'.": "\u4f7f\u7528 `is None` \u6216 `is not None`\u3002",
    "The PR can regress behavior without an automated signal catching it.": "\u8be5 PR \u53ef\u80fd\u5f15\u5165\u884c\u4e3a\u56de\u5f52\uff0c\u4f46\u6ca1\u6709\u81ea\u52a8\u5316\u6d4b\u8bd5\u4fe1\u53f7\u53ca\u65f6\u53d1\u73b0\u3002",
    "Add or update tests that cover the changed behavior, especially edge cases and failure paths.": "\u65b0\u589e\u6216\u66f4\u65b0\u8986\u76d6\u672c\u6b21\u53d8\u66f4\u884c\u4e3a\u7684\u6d4b\u8bd5\uff0c\u5c24\u5176\u662f\u8fb9\u754c\u6761\u4ef6\u548c\u5931\u8d25\u8def\u5f84\u3002",
    "A failing test suite means the branch is not safe to merge.": "\u6d4b\u8bd5\u5957\u4ef6\u5931\u8d25\u610f\u5473\u7740\u8be5\u5206\u652f\u5f53\u524d\u4e0d\u9002\u5408\u5408\u5165\u3002",
    "Fix the failing tests or update the implementation if the failures expose a regression.": "\u4fee\u590d\u5931\u8d25\u6d4b\u8bd5\uff1b\u5982\u679c\u5931\u8d25\u66b4\u9732\u4e86\u56de\u5f52\uff0c\u5219\u540c\u6b65\u4fee\u6b63\u5b9e\u73b0\u3002",
    "Review the PR diff and submit candidate findings with evidence.": "\u5ba1\u67e5 PR diff\uff0c\u5e76\u63d0\u4ea4\u5e26\u8bc1\u636e\u7684\u5019\u9009\u95ee\u9898\u3002",
    "Scope: Security risk reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u5b89\u5168\u98ce\u9669\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Scope: Correctness and edge-case reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u6b63\u786e\u6027\u548c\u8fb9\u754c\u6761\u4ef6\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Scope: Test coverage reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u6d4b\u8bd5\u8986\u76d6\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Scope: Maintainability reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u53ef\u7ef4\u62a4\u6027\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Challenge weak or merge-blocking findings before final resolution.": "\u5728\u6700\u7ec8\u88c1\u51b3\u524d\u8d28\u7591\u8bc1\u636e\u4e0d\u8db3\u6216\u963b\u585e\u5408\u5e76\u7684\u95ee\u9898\u3002",
    "Merge-blocking severity must be backed by concrete diff evidence and source context.": "\u963b\u585e\u5408\u5e76\u7ea7\u522b\u7684\u95ee\u9898\u5fc5\u987b\u6709\u660e\u786e diff \u8bc1\u636e\u548c\u6e90\u7801\u4e0a\u4e0b\u6587\u652f\u6491\u3002",
    "No challenge; evidence is sufficient.": "\u4e0d\u8d28\u7591\uff1b\u5f53\u524d\u8bc1\u636e\u5145\u5206\u3002",
    "Accepted after challenge because evidence chain is sufficient for the report.": "\u7ecf\u8d28\u7591\u540e\u63a5\u53d7\uff0c\u56e0\u4e3a\u8bc1\u636e\u94fe\u8db3\u4ee5\u8fdb\u5165\u6700\u7ec8\u62a5\u544a\u3002",
    "Accepted by lead reviewer after evidence review.": "Lead reviewer \u590d\u6838\u8bc1\u636e\u540e\u63a5\u53d7\u3002",
}


def zh_text(text: str) -> str:
    if text.startswith("Defense: "):
        return text.replace("Defense:", "\u7b54\u8fa9\uff1a").replace(
            "evidence item(s) include diff evidence and reviewer rationale.",
            "\u6761\u8bc1\u636e\u5305\u542b diff \u8bc1\u636e\u548c reviewer \u7406\u7531\u3002",
        )
    return ZH_TEXT.get(text, text)


class ReportWriter:
    def __init__(self, output_dir: Path, collector: FindingCollector, language: str = "zh"):
        self.output_dir = output_dir
        self.collector = collector
        self.language = language
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def verdict(self) -> str:
        severities = {finding.severity for finding in self.collector.findings}
        if "P0" in severities or "P1" in severities:
            return "request_changes"
        if severities:
            return "comment"
        return "approve"

    def summary(self) -> str:
        findings = self.collector.sorted()
        if not findings:
            if self.language == "zh":
                return "未在本次审查范围内发现阻塞合并的问题。"
            return "No blocking issues found in the reviewed diff."
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        parts = [f"{severity}: {counts[severity]}" for severity in sorted(counts)]
        if self.language == "zh":
            return f"共发现 {len(findings)} 个问题：" + "，".join(parts) + "。"
        return f"Found {len(findings)} issue(s): " + ", ".join(parts) + "."

    def payload(self) -> dict[str, Any]:
        verdict = self.verdict()
        if verdict not in VERDICTS:
            raise ValueError(f"Invalid verdict: {verdict}")
        return {
            "summary": self.summary(),
            "verdict": verdict,
            "findings": [asdict(finding) for finding in self.collector.sorted()],
        }

    def markdown(self) -> str:
        payload = self.payload()
        if self.language == "zh":
            return self._markdown_zh(payload)
        lines = [
            "# PR Code Review Agent Report",
            "",
            f"**Verdict:** `{payload['verdict']}`",
            "",
            f"**Summary:** {payload['summary']}",
            "",
            "## Findings",
            "",
        ]
        if not payload["findings"]:
            lines.append("No findings.")
        for idx, finding in enumerate(payload["findings"], start=1):
            lines.extend(
                [
                    f"### {idx}. [{finding['severity']}] {finding['title']}",
                    "",
                    f"- File: `{finding['file']}:{finding['line']}`",
                    f"- Category: `{finding['category']}`",
                    f"- Evidence: {finding['evidence']}",
                    f"- Impact: {finding['impact']}",
                    f"- Suggestion: {finding['suggestion']}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _markdown_zh(self, payload: dict[str, Any]) -> str:
        verdict_label = {
            "approve": "通过",
            "comment": "需要关注",
            "request_changes": "请求修改",
        }.get(payload["verdict"], payload["verdict"])
        lines = [
            "# PR 代码审查 Agent 报告",
            "",
            f"**结论：** `{payload['verdict']}`（{verdict_label}）",
            "",
            f"**摘要：** {payload['summary']}",
            "",
            "## 问题列表",
            "",
        ]
        if not payload["findings"]:
            lines.append("未发现问题。")
        for idx, finding in enumerate(payload["findings"], start=1):
            title = ZH_TITLE.get(finding["title"], finding["title"])
            category = ZH_CATEGORY.get(finding["category"], finding["category"])
            lines.extend(
                [
                    f"### {idx}. [{finding['severity']}] {title}",
                    "",
                    f"- 文件：`{finding['file']}:{finding['line']}`",
                    f"- 分类：`{category}`",
                    f"- 证据：{finding['evidence']}",
                    f"- 影响：{finding['impact']}",
                    f"- 建议：{finding['suggestion']}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def write(self) -> dict[str, str]:
        report_path = self.output_dir / REPORT_NAME
        findings_path = self.output_dir / FINDINGS_NAME
        report_path.write_text(self.markdown(), encoding="utf-8")
        findings_path.write_text(json.dumps(self.payload(), indent=2, ensure_ascii=False), encoding="utf-8")
        return {"report": str(report_path), "findings": str(findings_path)}


class ReportWriterAgent:
    def __init__(
        self,
        llm_client: LLMClient | None,
        transcript: Transcript,
        language: str = "zh",
    ):
        self.llm_client = llm_client
        self.transcript = transcript
        self.language = language

    def draft(self, report_context: dict[str, Any]) -> dict[str, Any]:
        payload = (
            self.llm_client.report_writer_review(report_context, self.language)
            if self.llm_client and self.llm_client.enabled
            else {}
        )
        draft = self._normalize(payload, report_context)
        self.transcript.emit(
            "report_writer.draft",
            llm_used=bool(payload),
            accepted=len(draft["accepted_findings"]),
            rejected=len(draft["rejected_findings"]),
            downgraded=len(draft["downgraded_findings"]),
        )
        return draft

    def _normalize(self, payload: dict[str, Any], report_context: dict[str, Any]) -> dict[str, Any]:
        fallback = self._fallback(report_context)
        if not isinstance(payload, dict):
            return fallback
        verdict = str(payload.get("verdict") or fallback["verdict"]).strip()
        if verdict not in VERDICTS:
            verdict = fallback["verdict"]
        accepted = self._normalize_standard_findings(payload.get("accepted_findings")) or fallback["accepted_findings"]
        rejected = self._normalize_standard_findings(payload.get("rejected_findings")) or fallback["rejected_findings"]
        downgraded = self._normalize_standard_findings(payload.get("downgraded_findings")) or fallback["downgraded_findings"]
        return {
            "report_style": "standardized",
            "verdict": verdict,
            "summary_points": self._list_of_strings(payload.get("summary_points")) or fallback["summary_points"],
            "accepted_findings": self._merge_policy_references(accepted, fallback["accepted_findings"]),
            "rejected_findings": self._merge_policy_references(rejected, fallback["rejected_findings"]),
            "downgraded_findings": self._merge_policy_references(downgraded, fallback["downgraded_findings"]),
            "duplicate_notes": self._list_of_strings(payload.get("duplicate_notes")) or fallback["duplicate_notes"],
            "critic_notes": self._list_of_strings(payload.get("critic_notes")) or fallback["critic_notes"],
        }

    def _fallback(self, report_context: dict[str, Any]) -> dict[str, Any]:
        records = report_context.get("council_records") or report_context.get("findings") or []
        accepted = [
            self._standard_finding(record, "Accepted by lead reviewer.")
            for record in records
            if record.get("status") == "accepted"
        ]
        downgraded = [
            self._standard_finding(record, "Downgraded by lead reviewer.")
            for record in records
            if record.get("status") == "downgraded"
        ]
        rejected = [
            self._standard_finding(record, "Rejected by lead reviewer.")
            for record in records
            if record.get("status") == "rejected"
        ]
        severities = {item["severity"] for item in accepted + downgraded}
        verdict = "request_changes" if severities & {"P0", "P1"} else ("comment" if severities else "approve")
        if accepted or downgraded:
            summary = [f"{len(accepted) + len(downgraded)} accepted or downgraded finding(s)."]
        else:
            summary = ["No blocking findings accepted."]
        duplicate_notes = [
            f"{record.get('finding_id')} rejected as duplicate/noise."
            for record in records
            if record.get("status") == "rejected" and "duplicate" in str(record.get("resolution_reason", "")).lower()
        ]
        critic_notes = [
            evidence.get("content", "")
            for record in records
            for evidence in record.get("evidence_chain", [])
            if evidence.get("source") in {"critic_review", "critic_challenge"}
        ][:12]
        return {
            "report_style": "standardized",
            "verdict": verdict,
            "summary_points": summary,
            "accepted_findings": accepted,
            "rejected_findings": rejected,
            "downgraded_findings": downgraded,
            "duplicate_notes": duplicate_notes,
            "critic_notes": critic_notes,
        }

    def _normalize_standard_findings(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized = []
        for item in value:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "issue": str(item.get("issue", "")).strip()[:300],
                    "severity": str(item.get("severity", "")).strip() if str(item.get("severity", "")).strip() in SEVERITIES else "P3",
                    "file": str(item.get("file", "")).strip(),
                    "line": int(item.get("line", 1) or 1),
                    "evidence": str(item.get("evidence", "")).strip()[:1200],
                    "impact": str(item.get("impact", "")).strip()[:1200],
                    "fix": str(item.get("fix", "")).strip()[:1200],
                    "why_accepted": str(item.get("why_accepted", "")).strip()[:1200],
                    "critic_notes": str(item.get("critic_notes", "")).strip()[:1200],
                    "policy_references": self._list_of_strings(item.get("policy_references"))[:5],
                }
            )
        return [item for item in normalized if item["issue"] and item["file"] and item["evidence"]]

    def _merge_policy_references(
        self,
        items: list[dict[str, Any]],
        fallback_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        fallback_by_location = {
            (item.get("file"), item.get("line")): item.get("policy_references", [])
            for item in fallback_items
        }
        for item in items:
            if item.get("policy_references"):
                continue
            item["policy_references"] = fallback_by_location.get((item.get("file"), item.get("line")), [])
        return items

    def _standard_finding(self, record: dict[str, Any], default_reason: str) -> dict[str, Any]:
        critic_notes = [
            evidence.get("content", "")
            for evidence in record.get("evidence_chain", [])
            if evidence.get("source") in {"critic_review", "critic_challenge"}
        ]
        policy_references = [
            evidence.get("content", "")
            for evidence in record.get("evidence_chain", [])
            if evidence.get("source") == "company_policy"
        ][:5]
        return {
            "issue": str(record.get("title", "")),
            "severity": str(record.get("severity", "P3")),
            "file": str(record.get("file", "")),
            "line": int(record.get("line", 1) or 1),
            "evidence": str(record.get("evidence", "")),
            "impact": str(record.get("impact", "")),
            "fix": str(record.get("suggestion", "")),
            "why_accepted": str(record.get("resolution_reason") or default_reason),
            "critic_notes": " ".join(critic_notes)[:1200],
            "policy_references": policy_references,
        }

    def _list_of_strings(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()]


class CouncilReportWriter:
    def __init__(
        self,
        output_dir: Path,
        collector: FindingCollector,
        language: str = "zh",
        council_records: list[dict[str, Any]] | None = None,
        council_messages: list[dict[str, Any]] | None = None,
        standardized_report: dict[str, Any] | None = None,
    ):
        self.output_dir = output_dir
        self.collector = collector
        self.language = language
        self.council_records = council_records or []
        self.council_messages = council_messages or []
        self.standardized_report = standardized_report or {}
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def verdict(self) -> str:
        severities = {finding.severity for finding in self.collector.findings}
        if "P0" in severities or "P1" in severities:
            return "request_changes"
        if severities:
            return "comment"
        return "approve"

    def summary(self) -> str:
        findings = self.collector.sorted()
        if not findings:
            if self.language == "zh":
                return "\u672a\u5728\u672c\u6b21\u5ba1\u67e5\u8303\u56f4\u5185\u53d1\u73b0\u963b\u585e\u5408\u5e76\u7684\u95ee\u9898\u3002"
            return "No blocking issues found in the reviewed diff."
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        parts = [f"{severity}: {counts[severity]}" for severity in sorted(counts)]
        if self.language == "zh":
            return f"\u5171\u53d1\u73b0 {len(findings)} \u4e2a\u95ee\u9898\uff1a" + "\uff0c".join(parts) + "\u3002"
        return f"Found {len(findings)} issue(s): " + ", ".join(parts) + "."

    def _payload_findings(self) -> list[dict[str, Any]]:
        if not self.council_records:
            return [asdict(finding) for finding in self.collector.sorted()]
        accepted = [
            record
            for record in self.council_records
            if record.get("status") in {"accepted", "downgraded"}
        ]
        rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        return sorted(
            accepted,
            key=lambda f: (
                rank.get(str(f.get("severity")), 99),
                str(f.get("file")),
                int(f.get("line", 0)),
            ),
        )

    def payload(self) -> dict[str, Any]:
        verdict = self.verdict()
        if verdict not in VERDICTS:
            raise ValueError(f"Invalid verdict: {verdict}")
        return {
            "summary": self.summary(),
            "verdict": verdict,
            "findings": self._payload_findings(),
            "standard_report": self.standardized_report,
            "council": {
                "messages": self.council_messages,
                "candidates": self.council_records,
            },
        }

    def markdown(self) -> str:
        payload = self.payload()
        if self.standardized_report:
            return self._standard_markdown(payload)
        if self.language == "zh":
            return self._markdown_zh(payload)
        lines = [
            "# PR Code Review Agent Report",
            "",
            f"**Verdict:** `{payload['verdict']}`",
            "",
            f"**Summary:** {payload['summary']}",
            "",
        ]
        self._append_council_process(lines, zh=False)
        self._append_findings(lines, payload, zh=False)
        return "\n".join(lines).rstrip() + "\n"

    def _standard_markdown(self, payload: dict[str, Any]) -> str:
        report = self.standardized_report
        zh = self.language == "zh"
        title = "# PR 代码审查标准化报告" if zh else "# Standardized PR Review Report"
        labels = {
            "verdict": "结论" if zh else "Verdict",
            "summary": "摘要" if zh else "Summary",
            "accepted": "接受的问题" if zh else "Accepted Findings",
            "downgraded": "降级的问题" if zh else "Downgraded Findings",
            "rejected": "拒绝的问题" if zh else "Rejected Findings",
            "duplicates": "重复/噪音说明" if zh else "Duplicate/Noise Notes",
            "critic": "Critic 说明" if zh else "Critic Notes",
            "evidence": "证据" if zh else "Evidence",
            "impact": "影响" if zh else "Impact",
            "fix": "修复建议" if zh else "Fix",
            "why": "接受理由" if zh else "Why",
        }
        lines = [
            title,
            "",
            f"**{labels['verdict']}:** `{report.get('verdict', payload['verdict'])}`",
            "",
            f"**Report Style:** `standardized`",
            "",
            f"## {labels['summary']}",
            "",
        ]
        summary_points = report.get("summary_points") or [payload["summary"]]
        lines.extend(f"- {point}" for point in summary_points)
        lines.append("")
        for section_key, section_label in (
            ("accepted_findings", labels["accepted"]),
            ("downgraded_findings", labels["downgraded"]),
            ("rejected_findings", labels["rejected"]),
        ):
            findings = report.get(section_key) or []
            lines.extend([f"## {section_label}", ""])
            if not findings:
                lines.append("无。" if zh else "None.")
                lines.append("")
                continue
            for idx, finding in enumerate(findings, start=1):
                lines.extend(
                    [
                        f"### {idx}. [{finding.get('severity')}] {finding.get('issue')}",
                        "",
                        f"- File: `{finding.get('file')}:{finding.get('line')}`",
                        f"- {labels['evidence']}: {finding.get('evidence')}",
                        f"- {labels['impact']}: {finding.get('impact')}",
                        f"- {labels['fix']}: {finding.get('fix')}",
                        f"- {labels['why']}: {finding.get('why_accepted')}",
                        f"- {labels['critic']}: {finding.get('critic_notes') or 'none'}",
                    ]
                )
                policy_references = finding.get("policy_references") or []
                if policy_references:
                    policy_label = "\u516c\u53f8\u89c4\u8303\u4f9d\u636e" if zh else "Company policy"
                    lines.append(f"- {policy_label}:")
                    lines.extend(f"  - {reference}" for reference in policy_references)
                lines.append("")
        for key, label in (("duplicate_notes", labels["duplicates"]), ("critic_notes", labels["critic"])):
            notes = report.get(key) or []
            lines.extend([f"## {label}", ""])
            if notes:
                lines.extend(f"- {note}" for note in notes)
            else:
                lines.append("无。" if zh else "None.")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _markdown_zh(self, payload: dict[str, Any]) -> str:
        verdict_label = {
            "approve": "\u901a\u8fc7",
            "comment": "\u9700\u8981\u5173\u6ce8",
            "request_changes": "\u8bf7\u6c42\u4fee\u6539",
        }.get(payload["verdict"], payload["verdict"])
        lines = [
            "# PR \u4ee3\u7801\u5ba1\u67e5 Agent \u62a5\u544a",
            "",
            f"**\u7ed3\u8bba\uff1a** `{payload['verdict']}`\uff08{verdict_label}\uff09",
            "",
            f"**\u6458\u8981\uff1a** {payload['summary']}",
            "",
        ]
        self._append_council_process(lines, zh=True)
        self._append_findings(lines, payload, zh=True)
        return "\n".join(lines).rstrip() + "\n"

    def _append_council_process(self, lines: list[str], zh: bool) -> None:
        if not self.council_messages:
            return
        lines.extend(["## " + ("\u5ba1\u67e5\u59d4\u5458\u4f1a\u8fc7\u7a0b" if zh else "Review Council Process"), ""])
        for message in self.council_messages:
            finding_suffix = f" ({message['finding_id']})" if message.get("finding_id") else ""
            content = zh_text(message["content"]) if zh else message["content"]
            lines.append(
                f"- `{message['type']}` {message['from']} -> {message['to']}{finding_suffix}: {content}"
            )
        lines.append("")

    def _append_findings(self, lines: list[str], payload: dict[str, Any], zh: bool) -> None:
        lines.extend(["## " + ("\u95ee\u9898\u5217\u8868" if zh else "Findings"), ""])
        if not payload["findings"]:
            lines.append("\u672a\u53d1\u73b0\u95ee\u9898\u3002" if zh else "No findings.")
            return
        for idx, finding in enumerate(payload["findings"], start=1):
            title = ZH_TITLE.get(finding["title"], finding["title"]) if zh else finding["title"]
            category = ZH_CATEGORY.get(finding["category"], finding["category"]) if zh else finding["category"]
            labels = {
                "file": "\u6587\u4ef6" if zh else "File",
                "category": "\u5206\u7c7b" if zh else "Category",
                "proposed": "\u63d0\u51fa\u8005" if zh else "Proposed by",
                "challenged": "\u8d28\u7591\u8005" if zh else "Challenged by",
                "resolution": "\u6700\u7ec8\u88c1\u51b3" if zh else "Resolution",
                "evidence": "\u8bc1\u636e" if zh else "Evidence",
                "impact": "\u5f71\u54cd" if zh else "Impact",
                "suggestion": "\u5efa\u8bae" if zh else "Suggestion",
                "chain": "\u8bc1\u636e\u94fe" if zh else "Evidence chain",
            }
            lines.extend(
                [
                    f"### {idx}. [{finding['severity']}] {title}",
                    "",
                    f"- {labels['file']}: `{finding['file']}:{finding['line']}`",
                    f"- {labels['category']}: `{category}`",
                    f"- Finding ID: `{finding.get('finding_id', '-')}`",
                    f"- {labels['proposed']}: `{finding.get('proposed_by', '-')}`",
                    f"- {labels['challenged']}: `{finding.get('challenged_by') or 'none'}`",
                    f"- {labels['resolution']}: `{finding.get('resolution', 'accepted')}`",
                    f"- {labels['evidence']}: {finding['evidence']}",
                    f"- {labels['impact']}: {zh_text(finding['impact']) if zh else finding['impact']}",
                    f"- {labels['suggestion']}: {zh_text(finding['suggestion']) if zh else finding['suggestion']}",
                    "",
                ]
            )
            evidence_chain = finding.get("evidence_chain") or []
            if evidence_chain:
                lines.append(f"  {labels['chain']}:")
                for evidence in evidence_chain:
                    evidence_content = zh_text(evidence.get("content", "")) if zh else evidence.get("content")
                    lines.append(
                        f"  - `{evidence.get('source')}` by `{evidence.get('added_by')}`: {evidence_content}"
                    )
                lines.append("")

    def write(self) -> dict[str, str]:
        report_path = self.output_dir / REPORT_NAME
        findings_path = self.output_dir / FINDINGS_NAME
        judge_input_path = self.output_dir / JUDGE_INPUT_NAME
        report_path.write_text(self.markdown(), encoding="utf-8")
        payload = self.payload()
        findings_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        judge_input = {
            "report_style": "standardized" if self.standardized_report else "legacy",
            "standard_report": self.standardized_report,
            "verdict": payload["verdict"],
            "summary": payload["summary"],
            "findings": payload["findings"],
            "council": payload["council"],
        }
        judge_input_path.write_text(json.dumps(judge_input, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"report": str(report_path), "findings": str(findings_path), "judge_input": str(judge_input_path)}


ReportWriter = CouncilReportWriter
