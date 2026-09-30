import asyncio
import json

from repopilot.core.models import LLMResponse
from repopilot.llm.scripted import ScriptedLLM
from repopilot.observability.events import EventBus
from repopilot.observability.usage import UsageTracker
from repopilot.planning.analyzer import TaskAnalyzer


def test_analyzer_validates_structured_specification() -> None:
    llm = ScriptedLLM([LLMResponse(text='''```json
    {"title":"Fix paging","description":"Remove duplicates across pages",
     "acceptance_criteria":["Adjacent pages have disjoint IDs","Tests pass"],
     "constraints":["Preserve API"],
     "behavior_cases":[{"key":"next_page","scenario":"a next page is requested",
       "expected":"IDs do not overlap the previous page","must_preserve":"ordering stays stable",
       "dimension":"boundary","state_effect":"cursor advances exactly once"}]}
    ```''')])
    result = asyncio.run(TaskAnalyzer(llm, EventBus("run"), UsageTracker()).analyze("paging bug", "pytest"))
    assert result.title == "Fix paging"
    assert len(result.acceptance_criteria) == 2
    assert result.behavior_cases[0].key == "next_page"
    assert result.behavior_cases[0].must_preserve == "ordering stays stable"
    assert result.behavior_cases[0].dimension == "boundary"
    assert result.behavior_cases[0].state_effect == "cursor advances exactly once"


def test_analyzer_uses_explicit_local_fallback() -> None:
    llm = ScriptedLLM([LLMResponse(text="not json")])
    result = asyncio.run(TaskAnalyzer(llm, EventBus("run"), UsageTracker()).analyze("Fix it", "pytest -q"))
    assert result.description == "Fix it"
    assert any("pytest -q" in item for item in result.acceptance_criteria)
    assert result.acceptance_criteria != ["Fix it"]
    assert result.behavior_cases[0].key == "issue_behavior"


def test_analyzer_rejects_unfocused_criteria_expansion() -> None:
    criteria = [f"Invented edge case {index}" for index in range(6)]
    llm = ScriptedLLM([LLMResponse(text=(
        '{"title":"Fix","description":"Fix behavior","acceptance_criteria":'
        + json.dumps(criteria) + ',"constraints":[]}'
    ))])
    result = asyncio.run(TaskAnalyzer(llm, EventBus("run"), UsageTracker()).analyze("Fix it", "pytest -q"))
    assert len(result.acceptance_criteria) == 3
    assert any("pytest -q" in item for item in result.acceptance_criteria)
