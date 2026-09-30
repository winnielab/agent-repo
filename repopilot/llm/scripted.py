"""Deterministic LLM used for protocol tests and reproducible E2E fixtures."""

from collections import deque
from typing import Any, Dict, Iterable, List, Optional

from repopilot.core.errors import ProviderResponseError
from repopilot.core.models import LLMResponse, Message


class ScriptedLLM:
    def __init__(self, responses: Iterable[LLMResponse]) -> None:
        self.responses = deque(responses)
        self.requests: List[tuple[List[Message], Optional[List[Dict[str, Any]]]]] = []

    async def generate(self, messages, tools=None) -> LLMResponse:
        self.requests.append((list(messages), tools))
        if not self.responses:
            raise ProviderResponseError("scripted LLM has no response left")
        return self.responses.popleft()

