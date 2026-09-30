import asyncio
import json

import pytest

from repopilot.core.errors import ProviderResponseError
from repopilot.core.models import Message
from repopilot.llm.openai_compatible import OpenAICompatibleClient


def client_with(payload, captured):
    def transport(url, headers, body, timeout):
        captured.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
        return json.dumps(payload).encode()

    return OpenAICompatibleClient("secret", "test-model", "https://example.invalid/v1", transport=transport)


def test_plain_response_and_usage() -> None:
    captured = {}
    client = client_with({
        "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                  "prompt_tokens_details": {"cached_tokens": 2}},
    }, captured)
    result = asyncio.run(client.generate([Message("user", "hi")]))
    assert result.text == "done"
    assert result.usage.to_dict() == {"input_tokens": 10, "output_tokens": 3, "cached_tokens": 2, "cost": None}
    assert "tools" not in captured["body"]


def test_multiple_native_tool_calls_are_parsed_and_schemas_sent() -> None:
    captured = {}
    payload = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
        {"id": "2", "function": {"name": "git_diff", "arguments": "{}"}},
    ]}, "finish_reason": "tool_calls"}]}
    client = client_with(payload, captured)
    result = asyncio.run(client.generate(
        [Message("user", "fix")],
        [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}],
    ))
    assert [call.name for call in result.tool_calls] == ["read_file", "git_diff"]
    assert captured["body"]["tools"][0]["function"]["name"] == "read_file"


def test_invalid_response_raises_stable_error() -> None:
    client = client_with({"unexpected": True}, {})
    with pytest.raises(ProviderResponseError):
        asyncio.run(client.generate([Message("user", "hi")]))
