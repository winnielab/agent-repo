"""Structured, revision-aware evidence for plan hypothesis checks."""

from __future__ import annotations

from typing import Any, Dict

from repopilot.core.models import ToolResult
from repopilot.tools.base import ToolContext, function_schema


class RecordEvidenceTool:
    """Record what a source trace or probe established without mutating files."""

    name = "record_evidence"
    description = (
        "Record a hypothesis-check result after inspecting source or running a focused probe. "
        "Candidate-semantic evidence is tied to the current workspace mutation revision."
    )

    def schema(self) -> Dict[str, Any]:
        return function_schema(self.name, self.description, {
            "type": "object",
            "properties": {
                "check_key": {"type": "string"},
                "verdict": {
                    "type": "string",
                    "enum": ["supported", "contradicted", "inconclusive"],
                },
                "observation": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 12,
                },
            },
            "required": ["check_key", "verdict", "observation", "evidence"],
            "additionalProperties": False,
        })

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        key = str(arguments["check_key"]).strip()
        verdict = str(arguments["verdict"]).strip()
        observation = str(arguments["observation"]).strip()
        evidence = [str(item).strip() for item in arguments["evidence"] if str(item).strip()]
        kinds = dict(context.values.get("hypothesis_check_kinds", {}))
        if key not in kinds:
            raise ValueError("check_key must name a current hypothesis check")
        if verdict not in {"supported", "contradicted", "inconclusive"}:
            raise ValueError("unsupported evidence verdict")
        if not observation or not evidence:
            raise ValueError("observation and at least one evidence reference are required")
        revision = int(context.values.get("mutation_revision", 0))
        if kinds[key] == "candidate_semantics" and revision < 1:
            return ToolResult(
                False,
                "Candidate semantics must be checked against an actual candidate patch after editing.",
                {"check_key": key, "mutation_revision": revision},
                "candidate_not_created",
            )
        provenance = []
        if kinds[key] == "candidate_semantics" and verdict == "supported":
            provenance = [
                item for item in context.values.get("command_evidence", [])
                if isinstance(item, dict)
                and item.get("evidence_for") == key
                and item.get("ok") is True
                and int(item.get("mutation_revision", -1)) == revision
            ]
            if not provenance:
                return ToolResult(
                    False,
                    (
                        "Supported candidate semantics require a successful bash probe tagged "
                        f"evidence_for={key!r} at mutation revision {revision}."
                    ),
                    {"check_key": key, "mutation_revision": revision},
                    "missing_probe_provenance",
                )
        record = {
            "check_key": key,
            "kind": kinds[key],
            "verdict": verdict,
            "observation": observation[:4000],
            "evidence": evidence,
            "mutation_revision": revision,
            "provenance": [
                {
                    "command": item.get("command", ""),
                    "returncode": item.get("returncode"),
                }
                for item in provenance[-3:]
            ],
        }
        records = context.values.setdefault("verification_evidence", [])
        records[:] = [item for item in records if item.get("check_key") != key]
        records.append(record)
        context.event_bus.emit("verification_evidence_recorded", **record)
        return ToolResult(True, f"Recorded {verdict} evidence for {key} at revision {revision}", record)
