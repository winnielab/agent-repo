"""Deterministic guards for explicit source-level behavioral contracts."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from repopilot.task.intent import is_test_path


_NORMATIVE = re.compile(
    r"\b(?:must|must not|should|should not|required|preserve|remain|propagate|unchanged)\b",
    re.IGNORECASE,
)
_MUST_PROPAGATE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Za-z0-9_]*(?:Error|Exception))\b"
    r"[^.\n;]{0,100}\b(?:must|should)\s+(?:still\s+)?propagate\b",
    re.IGNORECASE,
)
_MUST_NOT_CATCH = re.compile(
    r"\b(?:must|should)\s+not\s+(?:catch|suppress|hide)\s+"
    r"(?P<symbol>[A-Z][A-Za-z0-9_]*(?:Error|Exception))\b",
    re.IGNORECASE,
)
_TEMPORARY_ARTIFACT = re.compile(
    r"(?:^|/)(?:(?:debug|scratch|probe|repro(?:duce)?|experiment|manual[_-]?check|run)[_-][^/]+"
    r"\.(?:py|sh|txt|json)|[^/]+\.(?:log|out|tmp))$",
    re.IGNORECASE,
)


def extract_normative_contracts(files: Dict[str, str], limit: int = 200) -> List[Dict[str, Any]]:
    """Extract explicit normative source lines without asking an LLM to paraphrase them."""
    contracts: List[Dict[str, Any]] = []
    for path, source in files.items():
        for line_number, raw_line in enumerate(source.splitlines(), start=1):
            text = raw_line.strip().strip('"\'')
            if text and _NORMATIVE.search(text):
                contracts.append({"file": path, "line": line_number, "text": text[:500]})
                if len(contracts) >= limit:
                    return contracts
    return contracts


def _added_except_clauses(diff: str) -> Iterable[tuple[str, set[str], bool]]:
    for raw_line in diff.splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        line = raw_line[1:].strip()
        match = re.match(r"except\s*(?P<clause>[^:]*)\s*:", line)
        if not match:
            continue
        clause = match.group("clause").strip()
        broad = not clause or clause in {"Exception", "BaseException"}
        symbols = set(re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b", clause))
        yield line, symbols, broad


def contract_violations(diff: str, contracts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find candidate diff changes that directly contradict explicit contracts.

    The interface is rule-oriented so additional language-level guards can be
    added without placing benchmark or repository identifiers in the Agent.
    """
    required_to_propagate: Dict[str, Dict[str, Any]] = {}
    for contract in contracts:
        text = str(contract.get("text", ""))
        for pattern in (_MUST_PROPAGATE, _MUST_NOT_CATCH):
            for match in pattern.finditer(text):
                required_to_propagate[match.group("symbol")] = contract

    violations: List[Dict[str, Any]] = []
    seen = set()
    for added_line, caught, broad in _added_except_clauses(diff):
        conflicting = set(required_to_propagate) if broad else caught & set(required_to_propagate)
        for symbol in sorted(conflicting):
            key = (symbol, added_line)
            if key in seen:
                continue
            seen.add(key)
            contract = required_to_propagate[symbol]
            violations.append({
                "kind": "explicit_propagation_contract_caught",
                "symbol": symbol,
                "contract_file": contract.get("file", ""),
                "contract_line": contract.get("line", 0),
                "contract_text": contract.get("text", ""),
                "added_line": added_line,
            })
    return violations


def patch_hygiene_violations(diff: str, *, allow_test_changes: bool,
                             requested_text: str = "") -> List[Dict[str, Any]]:
    """Reject evaluation artifacts and out-of-contract test edits generically.

    This gate deliberately reasons about file roles, not benchmark instances or
    repository names. It complements the model prompt with a deterministic
    finalization boundary.
    """
    violations: List[Dict[str, Any]] = []
    current_path = ""
    new_file = False
    seen = set()

    def inspect_file() -> None:
        if not current_path:
            return
        if not allow_test_changes and is_test_path(current_path):
            key = ("unrequested_test_change", current_path)
            if key not in seen:
                seen.add(key)
                violations.append({
                    "kind": key[0],
                    "file": current_path,
                    "detail": "The task did not request changing test artifacts.",
                })
        explicitly_requested = (
            current_path.casefold() in requested_text.casefold()
            or current_path.rsplit("/", 1)[-1].casefold() in requested_text.casefold()
        )
        if new_file and not explicitly_requested and _TEMPORARY_ARTIFACT.search(current_path):
            key = ("temporary_artifact", current_path)
            if key not in seen:
                seen.add(key)
                violations.append({
                    "kind": key[0],
                    "file": current_path,
                    "detail": "A newly added scratch, reproduction, or command-output file leaked into the patch.",
                })

    for line in diff.splitlines():
        match = re.match(r"diff --git a/(.*?) b/(.*)$", line)
        if match:
            inspect_file()
            current_path = match.group(2)
            new_file = False
            continue
        if line.startswith("new file mode "):
            new_file = True
    inspect_file()
    return violations
