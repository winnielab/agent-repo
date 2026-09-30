import json
from pathlib import Path

from repopilot.core.models import LLMResponse, Message, ToolCall, Usage, json_safe


def test_models_are_json_safe() -> None:
    response = LLMResponse(
        text="",
        tool_calls=[ToolCall("call-1", "read_file", {"path": Path("a.py")})],
    )
    assert response.usage == Usage()
    assert response.to_dict()["tool_calls"][0]["arguments"] == {"path": "a.py"}
    json.dumps(response.to_dict())


def test_message_keeps_native_tool_protocol_fields() -> None:
    message = Message("tool", "done", tool_call_id="call-1")
    assert json_safe(message)["tool_call_id"] == "call-1"

