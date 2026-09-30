from __future__ import annotations

import json
from pathlib import Path

import pr_review_agent
from pr_review_agent import domain, reporting, settings, utils


def test_public_api_is_reassembled_from_component_modules() -> None:
    assert pr_review_agent.Finding is domain.Finding
    assert pr_review_agent.ReportWriter is reporting.CouncilReportWriter
    assert pr_review_agent.parse_numstat is utils.parse_numstat
    assert len(settings.TOOLS) == 10


def test_core_parsing_behavior_matches_legacy_contract() -> None:
    output = "12\t3\tsrc/app.py\n-\t-\tassets/logo.png\n"
    assert pr_review_agent.parse_numstat(output) == [
        {"file": "src/app.py", "added": 12, "deleted": 3},
        {"file": "assets/logo.png", "added": None, "deleted": None},
    ]


def test_offline_agent_can_be_assembled_and_run(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        'API_TOKEN = "fake_token_for_tests_12345"\n', encoding="utf-8"
    )
    agent = pr_review_agent.ReviewAgent(
        repo=tmp_path,
        base="main",
        target="HEAD",
        mode="simple",
        llm_provider="none",
        disable_company_rag=True,
    )

    result = agent.run()

    assert result["verdict"] == "request_changes"
    findings_path = Path(result["paths"]["findings"])
    assert findings_path.exists()
    payload = json.loads(findings_path.read_text(encoding="utf-8"))
    assert any(item["category"] == "security" for item in payload["findings"])
