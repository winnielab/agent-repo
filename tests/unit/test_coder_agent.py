from repopilot.agents.coder import CoderAgent
from repopilot.core.agent import Agent


def test_coder_is_declarative_agent_without_loop() -> None:
    coder = CoderAgent()
    assert isinstance(coder, Agent)
    assert "run_tests" in coder.tool_names
    assert "while" not in CoderAgent.__dict__
    assert "verify that exact symbol" in coder.system_prompt
    assert "newer version" in coder.system_prompt
    assert "earliest point" in coder.system_prompt
    assert "behavior-contract" in coder.system_prompt
    assert "benchmark instance identifiers" in coder.system_prompt
