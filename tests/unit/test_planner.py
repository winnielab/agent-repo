import asyncio

from repopilot.core.models import LLMResponse
from repopilot.llm.scripted import ScriptedLLM
from repopilot.observability.events import EventBus
from repopilot.observability.usage import UsageTracker
from repopilot.planning.planner import Planner
from repopilot.repo.explorer import RepositoryContext, RepositoryObservation
from repopilot.task.specification import BehaviorCase, TaskSpecification


def specification():
    return TaskSpecification("Fix", "Fix behavior", ["Behavior works"], ["Focused patch"])


def test_planner_folds_standalone_verification_into_fallback_task() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"summary":"two steps","steps":['
        '{"key":"implementation","title":"Implement","description":"Fix code","dependencies":[]},'
        '{"key":"tests","title":"Verify","description":"Run and adjust tests","dependencies":["implementation"]}]}')])
    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        specification(), RepositoryContext("python")))
    assert len(plan.steps) == 1
    assert plan.steps[0].key == "implementation"


def test_planner_invalid_output_falls_back_to_one_actionable_task() -> None:
    plan = asyncio.run(Planner(ScriptedLLM([LLMResponse(text="{}")] ), EventBus("run"), UsageTracker()).create_plan(
        specification(), RepositoryContext("python")))
    assert len(plan.steps) == 1 and plan.steps[0].key == "implement"


def test_planner_drops_unrequested_test_writing_and_rewires_dependencies() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"summary":"implementation plus test","steps":['
        '{"key":"implementation","title":"Implement fix","description":"Fix source code","dependencies":[]},'
        '{"key":"tests","title":"Add regression test","description":"Write a regression test","dependencies":["implementation"]},'
        '{"key":"cleanup","title":"Update documentation","description":"Document behavior","dependencies":["tests"]}]}')])
    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        specification(), RepositoryContext("python")))
    assert [step.key for step in plan.steps] == ["implementation", "cleanup"]
    assert plan.steps[1].dependencies == ["implementation"]


def test_planner_keeps_test_writing_when_issue_explicitly_requests_it() -> None:
    requested = TaskSpecification(
        "Add regression coverage", "Fix behavior and add a regression test",
        ["Behavior works"], ["Keep the patch focused"],
    )
    llm = ScriptedLLM([LLMResponse(text='{"summary":"implementation plus test","steps":['
        '{"key":"implementation","title":"Implement fix","description":"Fix source code","dependencies":[]},'
        '{"key":"tests","title":"Add regression test","description":"Write a regression test","dependencies":["implementation"]}]}')])
    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        requested, RepositoryContext("python")))
    assert [step.key for step in plan.steps] == ["implementation", "tests"]


def test_test_change_intent_distinguishes_verification_from_test_writing() -> None:
    verification_only = TaskSpecification(
        "Fix divide so all tests pass",
        "Repair the implementation without changing unrelated behavior",
        ["Tests pass"],
        ["Do not modify tests to make them pass"],
    )
    explicit_test_work = TaskSpecification(
        "Add coverage",
        "Add a regression test for the corrected behavior",
        ["Coverage exists"],
        [],
    )

    assert Planner._requests_test_changes(verification_only) is False
    assert Planner._requests_test_changes(explicit_test_work) is True


def test_planner_folds_inspection_and_confirmation_into_implementation() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"summary":"three steps","steps":['
        '{"key":"inspect","title":"Inspect implementation","description":"Identify the cause","dependencies":[]},'
        '{"key":"implementation","title":"Implement fix","description":"Fix source code","dependencies":["inspect"]},'
        '{"key":"confirm","title":"Confirm behavior","description":"Confirm the result","dependencies":["implementation"]}]}')])
    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        specification(), RepositoryContext("python")))
    assert [step.key for step in plan.steps] == ["implementation"]
    assert plan.steps[0].dependencies == []


def test_planner_folds_manually_verify_task_into_implementation() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"summary":"two steps","steps":['
        '{"key":"implementation","title":"Implement fix","description":"Fix source code","dependencies":[]},'
        '{"key":"manual","title":"Manually verify behavior","description":"Run a quick Python snippet","dependencies":["implementation"]}]}')])
    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        specification(), RepositoryContext("python")))
    assert [step.key for step in plan.steps] == ["implementation"]


def test_planner_requires_causal_contract_and_behavior_coverage() -> None:
    scoped = TaskSpecification(
        "Normalize input",
        "Preserve credentials while adding a missing scheme",
        ["Credentials survive normalization", "Tests pass"],
        ["Keep unrelated URLs unchanged"],
        [
            BehaviorCase("missing_scheme", "URL has no scheme", "scheme is added", "credentials survive"),
            BehaviorCase("has_scheme", "URL already has a scheme", "URL is unchanged"),
        ],
    )
    llm = ScriptedLLM([LLMResponse(text='''{
      "summary":"repair normalization",
      "root_cause_hypothesis":"userinfo is lost while normalizing the URL",
      "earliest_invariant":"normalization preserves parsed userinfo",
      "hypothesis_checks":[{"key":"trace-userinfo","claim":"normalization drops userinfo",
        "falsification":"trace a URL with userinfo through normalization and reject the claim if it survives",
        "evidence_keys":[]}],
      "steps":[{"key":"implementation","title":"Fix normalization",
        "description":"Preserve URL components at the normalization boundary",
        "dependencies":[],"behavior_keys":["missing_scheme","has_scheme"]}]
    }''')])

    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        scoped, RepositoryContext("python"),
    ))

    assert plan.root_cause_hypothesis == "userinfo is lost while normalizing the URL"
    assert plan.earliest_invariant == "normalization preserves parsed userinfo"
    assert plan.steps[0].behavior_keys == ["missing_scheme", "has_scheme"]


def test_planner_falls_back_when_behavior_case_is_uncovered() -> None:
    scoped = TaskSpecification(
        "Fix", "Fix both branches", ["Behavior works", "Tests pass"], [],
        [BehaviorCase("negative_case", "feature is disabled", "output stays unchanged")],
    )
    llm = ScriptedLLM([LLMResponse(text='''{
      "summary":"incomplete",
      "root_cause_hypothesis":"a branch is wrong",
      "earliest_invariant":"state is preserved",
      "steps":[{"key":"implementation","title":"Fix","description":"Fix code",
        "dependencies":[],"behavior_keys":[]}]
    }''')])

    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        scoped, RepositoryContext("python"),
    ))

    assert plan.steps[0].behavior_keys == ["negative_case"]
    assert "earliest" in plan.summary.casefold()


def test_planner_cites_task_focused_repository_evidence() -> None:
    scoped = TaskSpecification(
        "Fix parser", "Preserve state when parsing fails", ["Behavior works", "Tests pass"], [],
        [BehaviorCase(
            "failure_state", "parsing fails", "the original error propagates", "public API unchanged",
            "failure_path", "parser state is initialized before the error",
        )],
    )
    repository = RepositoryContext(
        "python", important_files=["parser.py"], structure_summary="parser",
        observations=[RepositoryObservation(
            "parser_definition", "definition", "parser.py", 10, "parse", "mutates parser state",
        )],
    )
    llm = ScriptedLLM([LLMResponse(text='''{
      "summary":"repair parser state transition",
      "root_cause_hypothesis":"state is initialized after the fallible conversion",
      "earliest_invariant":"state exists before conversion can fail",
      "impact_surface":["parse failure state","original exception"],
      "hypothesis_checks":[{"key":"trace-failure-state",
        "claim":"the conversion can fail before state initialization",
        "falsification":"trace the failure branch and reject the claim if state is initialized first",
        "evidence_keys":["parser_definition"]}],
      "steps":[{"key":"implementation","title":"Move state initialization",
        "description":"Initialize state at the parser boundary","dependencies":[],
        "behavior_keys":["failure_state"],"evidence_keys":["parser_definition"]}]
    }''')])

    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        scoped, repository,
    ))

    assert plan.steps[0].evidence_keys == ["parser_definition"]
    assert plan.impact_surface == ["parse failure state", "original exception"]
    assert plan.hypothesis_checks[0].key == "trace-failure-state"


def test_behavior_plan_requires_a_falsifiable_hypothesis_check() -> None:
    scoped = TaskSpecification(
        "Fix parser", "Preserve state when parsing fails", ["Behavior works"], [],
        [BehaviorCase("failure_state", "parsing fails", "state is preserved")],
    )
    llm = ScriptedLLM([LLMResponse(text='''{
      "summary":"missing check",
      "root_cause_hypothesis":"state is lost",
      "earliest_invariant":"state is preserved",
      "steps":[{"key":"implementation","title":"Fix","description":"Fix state",
        "dependencies":[],"behavior_keys":["failure_state"]}]
    }''')])

    plan = asyncio.run(Planner(llm, EventBus("run"), UsageTracker()).create_plan(
        scoped, RepositoryContext("python"),
    ))

    assert plan.steps[0].key == "implement"
    assert plan.hypothesis_checks[0].falsification
