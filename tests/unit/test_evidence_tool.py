import asyncio

from repopilot.execution.environment import LocalEnvironment
from repopilot.observability.events import EventBus
from repopilot.tools.base import ToolContext
from repopilot.tools.evidence import RecordEvidenceTool


def context(tmp_path, revision=0):
    return ToolContext(
        tmp_path,
        LocalEnvironment(tmp_path),
        "evidence",
        EventBus("evidence"),
        {
            "mutation_revision": revision,
            "hypothesis_check_keys": ["fact", "candidate"],
            "hypothesis_check_kinds": {
                "fact": "dependency_fact",
                "candidate": "candidate_semantics",
            },
        },
    )


def test_candidate_evidence_requires_a_candidate_revision(tmp_path) -> None:
    result = asyncio.run(RecordEvidenceTool().execute({
        "check_key": "candidate",
        "verdict": "supported",
        "observation": "value is preserved",
        "evidence": ["probe output"],
    }, context(tmp_path)))

    assert not result.ok
    assert result.error_code == "candidate_not_created"


def test_recorded_evidence_is_keyed_and_revision_aware(tmp_path) -> None:
    tool_context = context(tmp_path, revision=2)
    tool_context.values["command_evidence"] = [{
        "command": "python probe.py",
        "ok": True,
        "returncode": 0,
        "evidence_for": "candidate",
        "mutation_revision": 2,
    }]
    result = asyncio.run(RecordEvidenceTool().execute({
        "check_key": "candidate",
        "verdict": "supported",
        "observation": "positive and preservation values match",
        "evidence": ["python probe: cases=2", "api.py:10"],
    }, tool_context))

    assert result.ok
    assert tool_context.values["verification_evidence"] == [{
        "check_key": "candidate",
        "kind": "candidate_semantics",
        "verdict": "supported",
        "observation": "positive and preservation values match",
        "evidence": ["python probe: cases=2", "api.py:10"],
        "mutation_revision": 2,
        "provenance": [{"command": "python probe.py", "returncode": 0}],
    }]


def test_supported_candidate_evidence_requires_linked_successful_probe(tmp_path) -> None:
    result = asyncio.run(RecordEvidenceTool().execute({
        "check_key": "candidate",
        "verdict": "supported",
        "observation": "value is preserved",
        "evidence": ["unverified assertion"],
    }, context(tmp_path, revision=1)))

    assert not result.ok
    assert result.error_code == "missing_probe_provenance"
