"""Deterministic guards for explicit source-level behavioral contracts."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


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
