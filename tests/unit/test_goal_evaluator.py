import asyncio

from repopilot.agents.evaluator import GoalEvaluator
from repopilot.agents.reviewer import ReviewResult
from repopilot.core.models import LLMResponse
from repopilot.llm.scripted import ScriptedLLM
from repopilot.observability.events import EventBus
from repopilot.observability.usage import UsageTracker
from repopilot.planning.plan import HypothesisCheck, Plan, PlanStep
from repopilot.task.specification import BehaviorCase, TaskSpecification


def spec():
    return TaskSpecification("Fix", "Fix behavior", ["Boundary works", "API remains compatible"], [])


def test_goal_evaluator_checks_each_criterion() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"test"},'
        '{"criterion_index":1,"status":"pass","evidence":"diff"}],"summary":"done"}')])
    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(), "diff", {"ok": True}, ReviewResult("approve")))
    assert result.passed


def test_test_or_review_failure_overrides_model_pass() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"claim"},'
        '{"criterion_index":1,"status":"pass","evidence":"claim"}]}')])
    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(), "diff", {"ok": False, "output": "failure"}, ReviewResult("reject")))
    assert not result.passed
    assert "Configured tests pass" in result.repair_feedback()
    assert "Reviewer approves" in result.repair_feedback()


def test_evaluator_receives_bounded_repository_evidence() -> None:
    import json

    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"docstring"},'
        '{"criterion_index":1,"status":"pass","evidence":"diff"}]}')])
    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(), "diff", {"ok": True}, ReviewResult("approve"),
        {"important_file_contents": {"api.py": "TypeError must propagate"}},
    ))
    payload = json.loads(llm.requests[0][0][1].content)
    assert result.passed
    assert payload["repository_evidence"]["important_file_contents"]["api.py"] == "TypeError must propagate"


def test_evaluator_receives_compact_test_evidence() -> None:
    import json

    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"test"},'
        '{"criterion_index":1,"status":"pass","evidence":"diff"}]}')])
    noisy = "header\n" + "x" * 30_000 + "\nFAILED tests/test_api.py::test_case"
    asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(), "diff", {
            "ok": True,
            "stdout": noisy,
            "stderr": "",
            "output": noisy,
            "current_failure_ids": ["tests/test_api.py::test_case"],
            "new_failure_ids": ["tests/test_api.py::test_case"],
            "compatibility_errors": [{"module": "stale", "symbol": "value"}],
        },
        ReviewResult("approve"),
    ))
    payload = json.loads(llm.requests[0][0][1].content)
    assert "stdout" not in payload["tests"]
    assert len(payload["tests"]["output"]) <= 12_000
    assert payload["tests"]["output"].endswith("FAILED tests/test_api.py::test_case")
    assert payload["tests"]["current_failure_ids"] == []
    assert payload["tests"]["new_failure_ids"] == []
    assert payload["tests"]["compatibility_errors"] == []


def test_evaluator_prioritizes_target_version_compatibility_failure() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"claim"},'
        '{"criterion_index":1,"status":"pass","evidence":"claim"}]}')])
    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(),
        "diff",
        {
            "ok": False,
            "output": "AttributeError while collecting tests",
            "compatibility_errors": [{
                "kind": "missing_attribute",
                "module": "framework.nodes",
                "symbol": "NewNode",
            }],
        },
        ReviewResult("approve"),
    ))

    feedback = result.repair_feedback()
    assert not result.passed
    assert "Target-version compatibility failure" in feedback
    assert "framework.nodes.NewNode" in feedback


def test_evaluator_turns_behavior_counterexample_into_repair_contract() -> None:
    behavior_spec = TaskSpecification(
        "Normalize", "Preserve URL parts", ["Normalization works", "Tests pass"], [],
        [BehaviorCase("credentials", "URL contains credentials", "credentials survive normalization")],
    )
    llm = ScriptedLLM([LLMResponse(text='''{
      "criteria":[
        {"criterion_key":"acceptance:0","status":"pass","evidence":"diff"},
        {"criterion_key":"acceptance:1","status":"pass","evidence":"tests"},
        {"criterion_key":"behavior:credentials","status":"fail",
         "evidence":"the parser output drops userinfo",
         "expected":"credentials survive normalization",
         "observed":"userinfo is empty after normalization",
         "counterexample":"a credential-bearing URL without a scheme",
         "invalidated_assumption":"adding credentials later is equivalent to preserving them"}
      ],"summary":"one behavior remains broken"
    }''')])

    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        behavior_spec, "diff", {"ok": True}, ReviewResult("approve"),
    ))

    assert not result.passed
    feedback = result.repair_feedback()
    assert "Expected: credentials survive normalization" in feedback
    assert "Counterexample: a credential-bearing URL without a scheme" in feedback
    assert "Invalidated assumption: adding credentials later" in feedback


def test_evaluator_does_not_silently_skip_behavior_contract() -> None:
    behavior_spec = TaskSpecification(
        "Fix", "Keep disabled behavior", ["Behavior works", "Tests pass"], [],
        [BehaviorCase("disabled", "feature is disabled", "output is unchanged")],
    )
    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"diff"},'
        '{"criterion_index":1,"status":"pass","evidence":"tests"}]}')])

    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        behavior_spec, "diff", {"ok": True}, ReviewResult("approve"),
    ))

    missing = next(item for item in result.criteria if item.key == "behavior:disabled")
    assert missing.status == "unknown"
    assert not result.passed


def test_explicit_source_contract_overrides_plausible_evaluator_pass() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"diff"},'
        '{"criterion_index":1,"status":"pass","evidence":"tests"}]}')])
    diff = """diff --git a/parser.py b/parser.py
--- a/parser.py
+++ b/parser.py
@@ -1 +1 @@
-    except ValueError:
+    except (TypeError, ValueError):
"""
    evidence = {"normative_contracts": [{
        "file": "parser.py",
        "line": 2,
        "text": "TypeError and unrelated exceptions must propagate.",
    }]}

    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(), diff, {"ok": True}, ReviewResult("approve"), evidence,
    ))

    violation = next(item for item in result.criteria if item.key.startswith("contract:"))
    assert violation.status == "fail"
    assert "TypeError" in violation.counterexample
    assert not result.passed


def test_patch_hygiene_overrides_plausible_evaluator_pass() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"criteria":['
        '{"criterion_index":0,"status":"pass","evidence":"diff"},'
        '{"criterion_index":1,"status":"pass","evidence":"tests"}]}')])
    diff = """diff --git a/debug_probe.py b/debug_probe.py
new file mode 100644
--- /dev/null
+++ b/debug_probe.py
@@ -0,0 +1 @@
+print('debug')
"""

    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(), diff, {"ok": True}, ReviewResult("approve"),
    ))

    violation = next(item for item in result.criteria if item.key.startswith("patch_hygiene:"))
    assert violation.status == "fail"
    assert violation.counterexample == "debug_probe.py"
    assert not result.passed


def test_evaluator_requires_hypothesis_check_evidence() -> None:
    plan = Plan(
        "trace then fix",
        [PlanStep("fix", "Fix parser", "Fix the earliest violation")],
        "normalization drops state",
        "state survives normalization",
        [],
        [HypothesisCheck(
            "trace-state",
            "normalization drops state before the caller observes it",
            "run a focused input through normalization and inspect the caller-visible state",
        )],
    )
    llm = ScriptedLLM([LLMResponse(text='''{
      "criteria":[
        {"criterion_key":"acceptance:0","status":"pass","evidence":"diff"},
        {"criterion_key":"acceptance:1","status":"pass","evidence":"tests"},
        {"criterion_key":"hypothesis:trace-state","status":"unknown",
         "evidence":"no focused probe was recorded",
         "invalidated_assumption":"the planned root cause was actually observed"}
      ],"summary":"hypothesis remains unverified"
    }''')])

    result = asyncio.run(GoalEvaluator(llm, EventBus("run"), UsageTracker()).evaluate(
        spec(), "diff", {"ok": True}, ReviewResult("approve"), {}, plan,
    ))

    check = next(item for item in result.criteria if item.key == "hypothesis:trace-state")
    assert check.status == "unknown"
    assert not result.passed
