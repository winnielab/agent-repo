"""Composable lifecycle hooks that cannot override permission decisions."""

from __future__ import annotations

import inspect
from collections import defaultdict
from enum import Enum
from typing import Any, Awaitable, Callable, DefaultDict, Dict, List, Union


class HookEvent(Enum):
    BEFORE_LLM_CALL = "BeforeLLMCall"
    AFTER_LLM_CALL = "AfterLLMCall"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


Hook = Callable[[Dict[str, Any]], Union[None, Awaitable[None]]]


class HookManager:
    def __init__(self) -> None:
        self._hooks: DefaultDict[HookEvent, List[Hook]] = defaultdict(list)

    def register(self, event: HookEvent, hook: Hook) -> None:
        self._hooks[event].append(hook)

    async def run(self, event: HookEvent, **payload: Any) -> None:
        for hook in list(self._hooks[event]):
            result = hook(payload)
            if inspect.isawaitable(result):
                await result

