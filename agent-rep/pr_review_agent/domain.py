"""Modularized domain component of the PR review agent."""

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

from .settings import CATEGORIES, SEVERITIES
from .utils import now, truncate


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    severity: str
    category: str
    title: str
    evidence: str
    impact: str
    suggestion: str

    def validate(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"Invalid severity: {self.severity}")
        if self.category not in CATEGORIES:
            raise ValueError(f"Invalid category: {self.category}")
        if not self.file:
            raise ValueError("Finding file is required")
        if self.line < 1:
            raise ValueError("Finding line must be >= 1")
        for field in ("title", "evidence", "impact", "suggestion"):
            if not getattr(self, field).strip():
                raise ValueError(f"Finding {field} is required")


class Transcript:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def emit(self, event: str, **payload: Any) -> None:
        row = {"ts": now(), "event": event, **payload}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class FindingCollector:
    def __init__(self):
        self.findings: list[Finding] = []
        self._keys: set[tuple[str, int, str, str]] = set()

    def emit(self, finding: Finding | dict[str, Any]) -> str:
        item = finding if isinstance(finding, Finding) else Finding(**finding)
        item.validate()
        key = (item.file, item.line, item.category, item.title)
        if key in self._keys:
            return "Duplicate finding ignored"
        self._keys.add(key)
        self.findings.append(item)
        return f"Recorded {item.severity} {item.category}: {item.title}"

    def sorted(self) -> list[Finding]:
        rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        return sorted(self.findings, key=lambda f: (rank[f.severity], f.file, f.line))


VALID_MESSAGE_TYPES = {
    "task_assignment",
    "evidence_request",
    "candidate_finding",
    "challenge",
    "defense",
    "resolution",
}


@dataclass(frozen=True)
class CouncilMessage:
    sender: str
    to: str
    msg_type: str
    content: str
    finding_id: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.sender,
            "to": self.to,
            "type": self.msg_type,
            "content": self.content,
            "finding_id": self.finding_id,
            "timestamp": self.timestamp,
        }


class MessageBus:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self.messages: list[CouncilMessage] = []

    def send(self, sender: str, to: str, msg_type: str, content: str, finding_id: str = "") -> CouncilMessage:
        if msg_type not in VALID_MESSAGE_TYPES:
            raise ValueError(f"Invalid message type: {msg_type}")
        message = CouncilMessage(
            sender=sender,
            to=to,
            msg_type=msg_type,
            content=content,
            finding_id=finding_id,
            timestamp=now(),
        )
        self.messages.append(message)
        self.transcript.emit("council.message", **message.to_dict())
        return message

    def read(self, name: str) -> list[CouncilMessage]:
        return [message for message in self.messages if message.to == name]

    def all(self) -> list[dict[str, Any]]:
        return [message.to_dict() for message in self.messages]


@dataclass(frozen=True)
class EvidenceItem:
    source: str
    content: str
    added_by: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceStore:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self._items: dict[str, list[EvidenceItem]] = {}

    def add(self, finding_id: str, source: str, content: str, added_by: str) -> None:
        item = EvidenceItem(
            source=source,
            content=truncate(str(content), 2000),
            added_by=added_by,
            timestamp=now(),
        )
        self._items.setdefault(finding_id, []).append(item)
        self.transcript.emit("council.evidence", finding_id=finding_id, **item.to_dict())

    def list(self, finding_id: str) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._items.get(finding_id, [])]


@dataclass
class CouncilFinding:
    finding_id: str
    finding: Finding
    status: str
    proposed_by: str
    challenged_by: str = ""
    resolution: str = "candidate"
    resolution_reason: str = ""

    def to_dict(self, evidence_chain: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            **asdict(self.finding),
            "finding_id": self.finding_id,
            "status": self.status,
            "proposed_by": self.proposed_by,
            "challenged_by": self.challenged_by,
            "resolution": self.resolution,
            "resolution_reason": self.resolution_reason,
            "evidence_chain": evidence_chain,
        }


class FindingLifecycle:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self._items: dict[str, CouncilFinding] = {}
        self._keys: dict[tuple[str, int, str, str], str] = {}
        self._next_id = 1

    def candidate(self, finding: Finding, proposed_by: str) -> CouncilFinding:
        finding.validate()
        key = (finding.file, finding.line, finding.category, finding.title)
        existing_id = self._keys.get(key)
        if existing_id:
            existing = self._items[existing_id]
            self.transcript.emit(
                "council.duplicate_candidate",
                existing_id=existing_id,
                proposed_by=proposed_by,
            )
            return existing
        finding_id = f"F-{self._next_id:03d}"
        self._next_id += 1
        item = CouncilFinding(
            finding_id=finding_id,
            finding=finding,
            status="candidate",
            proposed_by=proposed_by,
        )
        self._items[finding_id] = item
        self._keys[key] = finding_id
        self.transcript.emit("council.finding.candidate", finding_id=finding_id, proposed_by=proposed_by)
        return item

    def challenge(self, finding_id: str, challenged_by: str, reason: str) -> CouncilFinding:
        item = self._items[finding_id]
        item.status = "challenged"
        item.challenged_by = challenged_by
        item.resolution = "challenged"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.challenge", finding_id=finding_id, challenged_by=challenged_by)
        return item

    def accept(self, finding_id: str, reason: str) -> CouncilFinding:
        item = self._items[finding_id]
        item.status = "accepted"
        item.resolution = "accepted"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.accept", finding_id=finding_id, reason=reason)
        return item

    def reject(self, finding_id: str, reason: str) -> CouncilFinding:
        item = self._items[finding_id]
        item.status = "rejected"
        item.resolution = "rejected"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.reject", finding_id=finding_id, reason=reason)
        return item

    def downgrade(self, finding_id: str, severity: str, reason: str) -> CouncilFinding:
        if severity not in SEVERITIES:
            raise ValueError(f"Invalid severity: {severity}")
        item = self._items[finding_id]
        item.finding = Finding(
            file=item.finding.file,
            line=item.finding.line,
            severity=severity,
            category=item.finding.category,
            title=item.finding.title,
            evidence=item.finding.evidence,
            impact=item.finding.impact,
            suggestion=item.finding.suggestion,
        )
        item.status = "downgraded"
        item.resolution = "downgraded"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.downgrade", finding_id=finding_id, severity=severity)
        return item

    def revise(self, finding_id: str, finding: Finding, reason: str) -> CouncilFinding:
        finding.validate()
        item = self._items[finding_id]
        old_key = (item.finding.file, item.finding.line, item.finding.category, item.finding.title)
        self._keys.pop(old_key, None)
        item.finding = finding
        item.status = "candidate"
        item.resolution = "revised"
        item.resolution_reason = reason
        self._keys[(finding.file, finding.line, finding.category, finding.title)] = finding_id
        self.transcript.emit("council.finding.revise", finding_id=finding_id, reason=reason)
        return item

    def accepted(self) -> list[CouncilFinding]:
        return [item for item in self._items.values() if item.status in {"accepted", "downgraded"}]

    def all(self) -> list[CouncilFinding]:
        return list(self._items.values())


class TodoManager:
    def __init__(self):
        self.items: list[dict[str, str]] = []

    def update(self, items: list[dict[str, str]]) -> str:
        in_progress = 0
        normalized = []
        for item in items:
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip()
            if not content:
                raise ValueError("Todo content is required")
            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"Invalid todo status: {status}")
            if status == "in_progress":
                in_progress += 1
            normalized.append({"content": content, "status": status})
        if in_progress > 1:
            raise ValueError("Only one todo can be in_progress")
        self.items = normalized
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        return "\n".join(f"{marker[i['status']]} {i['content']}" for i in self.items)
