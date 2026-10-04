import asyncio
import json

import pytest

from repopilot.core.errors import ProviderHTTPError, ProviderResponseError
from repopilot.core.models import LLMResponse, Message
from repopilot.llm.openai_compatible import (
    ExhaustionFallbackClient,
    OpenAICompatibleClient,
)


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


class StubClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def generate(self, messages, tools=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_credit_exhaustion_switches_permanently_to_fallback() -> None:
    primary = StubClient([
        ProviderHTTPError("provider HTTP 403: account balance is insufficient"),
    ])
    fallback = StubClient([LLMResponse(text="first"), LLMResponse(text="second")])
    activations = []
    client = ExhaustionFallbackClient(
        primary, fallback, on_fallback=lambda: activations.append(True),
    )

    first = asyncio.run(client.generate([Message("user", "one")]))
    second = asyncio.run(client.generate([Message("user", "two")]))

    assert (first.text, second.text) == ("first", "second")
    assert primary.calls == 1
    assert fallback.calls == 2
    assert activations == [True]


def test_rate_limit_does_not_switch_provider() -> None:
    error = ProviderHTTPError(
        "provider HTTP 429: Request was rejected due to rate limiting; TPM limit reached",
    )
    primary = StubClient([error])
    fallback = StubClient([LLMResponse(text="must not run")])
    client = ExhaustionFallbackClient(primary, fallback, max_primary_attempts=1)

    with pytest.raises(ProviderHTTPError, match="429"):
        asyncio.run(client.generate([Message("user", "one")]))

    assert not client.using_fallback
    assert fallback.calls == 0


def test_transient_rate_limit_retries_primary_without_fallback() -> None:
    error = ProviderHTTPError(
        "provider HTTP 429: System is too busy now",
    )
    primary = StubClient([error, LLMResponse(text="primary recovered")])
    fallback = StubClient([LLMResponse(text="must not run")])
    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    client = ExhaustionFallbackClient(
        primary,
        fallback,
        initial_retry_seconds=2,
        sleep=no_sleep,
    )
    result = asyncio.run(client.generate([Message("user", "one")]))

    assert result.text == "primary recovered"
    assert primary.calls == 2
    assert fallback.calls == 0
    assert delays == [2]


def test_provider_timeout_retries_primary_without_fallback() -> None:
    from repopilot.core.errors import ProviderTimeoutError

    primary = StubClient([
        ProviderTimeoutError("provider timed out after 60s"),
        LLMResponse(text="primary recovered"),
    ])
    fallback = StubClient([LLMResponse(text="must not run")])
    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    client = ExhaustionFallbackClient(primary, fallback, sleep=no_sleep)
    result = asyncio.run(client.generate([Message("user", "one")]))

    assert result.text == "primary recovered"
    assert fallback.calls == 0
    assert delays == [2]
