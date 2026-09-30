"""Provider-neutral LLM interface."""

from typing import Any, Dict, List, Optional, Protocol

from repopilot.core.models import LLMResponse, Message


class LLMClient(Protocol):
    async def generate(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        ...

