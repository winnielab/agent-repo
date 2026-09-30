"""OpenAI-compatible chat-completions transport, including DeepSeek."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from repopilot.core.errors import (
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from repopilot.core.models import LLMResponse, Message, ToolCall, Usage

Transport = Callable[[str, Dict[str, str], bytes, float], bytes]


def _urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise ProviderHTTPError(f"provider HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderTimeoutError(f"provider timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise ProviderTimeoutError(f"provider timed out after {timeout}s") from exc
        raise ProviderHTTPError(f"provider connection failed: {exc.reason}") from exc


class OpenAICompatibleClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 60.0,
        transport: Optional[Transport] = None,
        temperature: float = 0.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or _urllib_transport
        self.temperature = temperature

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    @staticmethod
    def _message_payload(message: Message) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return payload

    @staticmethod
    def _parse_tool_call(item: Any) -> ToolCall:
        try:
            function = item["function"]
            raw_arguments = function.get("arguments", "{}")
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments are not an object")
            return ToolCall(id=str(item["id"]), name=str(function["name"]), arguments=arguments)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderResponseError(f"invalid tool call: {exc}") from exc

    @classmethod
    def parse_response(cls, payload: Any) -> LLMResponse:
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            usage_payload = payload.get("usage") or {}
            cache_details = usage_payload.get("prompt_tokens_details") or {}
            tool_calls = [cls._parse_tool_call(item) for item in message.get("tool_calls") or []]
            usage = Usage(
                input_tokens=int(usage_payload.get("prompt_tokens") or 0),
                output_tokens=int(usage_payload.get("completion_tokens") or 0),
                cached_tokens=int(cache_details.get("cached_tokens") or 0),
            )
            return LLMResponse(
                text=message.get("content") or "",
                tool_calls=tool_calls,
                usage=usage,
                stop_reason=choice.get("finish_reason"),
                raw=payload,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderResponseError(f"invalid provider response: {exc}") from exc

    async def generate(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [self._message_payload(message) for message in messages],
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [
                schema if schema.get("type") == "function" else {"type": "function", "function": schema}
                for schema in tools
            ]
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        try:
            raw = await asyncio.to_thread(self.transport, self.endpoint, headers, body, self.timeout)
        except ProviderHTTPError:
            raise
        except ProviderTimeoutError:
            raise
        except TimeoutError as exc:
            raise ProviderTimeoutError(f"provider timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise ProviderHTTPError(f"provider request failed: {exc}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderResponseError(f"provider returned invalid JSON: {exc}") from exc
        return self.parse_response(decoded)


def deepseek_from_environment(model: str = "deepseek-chat") -> OpenAICompatibleClient:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise ValueError("DEEPSEEK_API_KEY is not set")
    return OpenAICompatibleClient(
        api_key=key,
        model=model,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )

