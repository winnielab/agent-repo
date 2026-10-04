"""Validated patch merge with same-file hunk classification and final verification."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from repopilot.core.models import Serializable
from repopilot.execution.git import run_git
from repopilot.observability.events import EventBus


@dataclass(frozen=True)
class PatchArtifact(Serializable):
    task_id: str
    patch_path: Path
    patch_sha256: str
    base_commit: str
    result_commit: Optional[str] = None

    @classmethod
    def from_path(cls, task_id: str, patch_path: Path, base_commit: str,
                  result_commit: Optional[str] = None) -> "PatchArtifact":
        path = Path(patch_path)
        content = path.read_bytes()
        return cls(task_id, path, hashlib.sha256(content).hexdigest(), base_commit, result_commit)


@dataclass
class MergeResult(Serializable):
    status: str
    classification: str
    merged_tasks: List[str] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    tests: Dict[str, object] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "completed" and bool(self.tests.get("ok", True))


VerificationCallable = Callable[[Path], Awaitable[Dict[str, object]]]
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_hunks(text: str) -> Dict[str, List[Tuple[int, int]]]:
    hunks: Dict[str, List[Tuple[int, int]]] = {}
    current: Optional[str] = None
    old_path: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("--- "):
            value = line[4:].split("\t", 1)[0]
            old_path = None if value == "/dev/null" else (
                value[2:] if value.startswith("a/") else value
            )
            continue
        if line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0]
            if value == "/dev/null":
                current = old_path
            else:
                current = value[2:] if value.startswith("b/") else value
            if current:
                hunks.setdefault(current, [])
            continue
        match = _HUNK.match(line)
        if match and current:
            start = int(match.group(1))
            length = int(match.group(2) or "1")
            # Zero-length insertions occupy a point in the base coordinate system.
            hunks[current].append((start, start + max(length, 1) - 1))
    return hunks


def _overlap(left: Tuple[int, int], right: Tuple[int, int]) -> bool:
    return max(left[0], right[0]) <= min(left[1], right[1])


class MergeManager:
    def __init__(self, target: Path, *, event_bus: Optional[EventBus] = None,
                 verifier: Optional[VerificationCallable] = None) -> None:
        self.target = Path(target).resolve()
        self.event_bus = event_bus or EventBus("merge")
        self.verifier = verifier

    def _validate(self, artifacts: List[PatchArtifact]) -> tuple[str, List[str]]:
        if not artifacts:
            raise ValueError("at least one patch artifact is required")
        head = run_git(self.target, ["rev-parse", "HEAD"]).strip()
        hunks_by_file: Dict[str, List[tuple[str, Tuple[int, int]]]] = {}
        same_file = False
        conflicts: List[str] = []
        for artifact in artifacts:
            path = Path(artifact.patch_path)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != artifact.patch_sha256:
                raise ValueError(f"patch digest mismatch for {artifact.task_id}")
            if artifact.base_commit != head:
                raise ValueError(
                    f"base commit mismatch for {artifact.task_id}: "
                    f"expected {head}, got {artifact.base_commit}"
                )
            if not content.strip():
                raise ValueError(f"empty patch for {artifact.task_id}")
            for filename, ranges in _patch_hunks(content.decode("utf-8", errors="replace")).items():
                prior = hunks_by_file.setdefault(filename, [])
                if prior:
                    same_file = True
                for new_range in ranges:
                    for prior_task, prior_range in prior:
                        if _overlap(new_range, prior_range):
                            conflicts.append(
                                f"{filename}: {prior_task}{prior_range} overlaps "
                                f"{artifact.task_id}{new_range}"
                            )
                    prior.append((artifact.task_id, new_range))
        if conflicts:
            return "same_file_conflict", conflicts
        return ("same_file_non_overlapping" if same_file else "disjoint"), []

    async def merge(self, artifacts: List[PatchArtifact]) -> MergeResult:
        self.event_bus.emit("merge_started", tasks=[item.task_id for item in artifacts])
        try:
            classification, conflicts = self._validate(artifacts)
            if conflicts:
                result = MergeResult("conflict", classification, conflicts=conflicts,
                                     error="overlapping same-file hunks")
                self.event_bus.emit("merge_finished", **result.to_dict())
                return result
            paths = [str(Path(item.patch_path).resolve()) for item in artifacts]
            # Checking all patches in one invocation prevents a partially applied merge.
            run_git(self.target, ["apply", "--check", "--binary", "--", *paths])
            run_git(self.target, ["apply", "--binary", "--", *paths])
            raw_status = run_git(
                self.target, ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )
            changed_files: List[str] = []
            parts = raw_status.split("\0")
            index = 0
            while index < len(parts):
                row = parts[index]
                index += 1
                if not row:
                    continue
                status, filename = row[:2], row[3:]
                if status[0] in {"R", "C"} and index < len(parts) and parts[index]:
                    filename = parts[index]
                    index += 1
                changed_files.append(filename)
            changed_files = sorted(set(changed_files))
            tests = await self.verifier(self.target) if self.verifier else {"ok": True}
            status = "completed" if bool(tests.get("ok", False)) else "verification_failed"
            result = MergeResult(
                status, classification, [item.task_id for item in artifacts],
                changed_files, tests=tests,
                error=None if status == "completed" else "post-merge verification failed",
            )
        except Exception as exc:
            result = MergeResult(
                "failed", "invalid", error=f"{type(exc).__name__}: {exc}",
            )
        self.event_bus.emit("merge_finished", **result.to_dict())
        return result
