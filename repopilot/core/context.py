"""Bounded conversation storage with persisted large tool results."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from repopilot.core.models import AgentTask, Message, ToolCall
from repopilot.observability.events import EventBus


class ContextManager:
    def __init__(
        self,
        system_prompt: str,
        task: AgentTask,
        artifact_dir: Path,
        event_bus: EventBus,
        char_budget: int = 120_000,
        large_result_chars: int = 20_000,
        keep_recent_messages: int = 12,
    ) -> None:
        self.system_prompt = system_prompt
        self.task = task
        self.artifact_dir = Path(artifact_dir)
        self.event_bus = event_bus
        self.char_budget = char_budget
        self.large_result_chars = large_result_chars
        self.keep_recent_messages = keep_recent_messages
        self.messages: List[Message] = []
        self.important_observations: List[str] = []
        self.todo_state: List[str] = []
        self.history_summary = ""

    def add_assistant(self, text: str, tool_calls: List[ToolCall]) -> None:
        self.messages.append(Message("assistant", text, tool_calls=tool_calls))

    def add_tool_result(self, call: ToolCall, content: str) -> None:
        if len(content) > self.large_result_chars:
            directory = self.artifact_dir / "tool-results"
            directory.mkdir(parents=True, exist_ok=True)
            safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", call.id)[:100] or "call"
            path = directory / f"{safe_id}.txt"
            path.write_text(content, encoding="utf-8")
            preview = content[:2000]
            content = f"<persisted-output>\nFull output: {path}\nPreview:\n{preview}\n</persisted-output>"
        self.messages.append(Message("tool", content, tool_call_id=call.id))

    def add_observation(self, text: str) -> None:
        if text and text not in self.important_observations:
            self.important_observations.append(text)

    def build_context(self) -> List[Message]:
        result = [Message("system", self.system_prompt), Message("user", self.task.description)]
        state_parts = []
        if self.history_summary:
            state_parts.append("History summary:\n" + self.history_summary)
        if self.important_observations:
            state_parts.append("Important observations:\n- " + "\n- ".join(self.important_observations[-20:]))
        if self.todo_state:
            state_parts.append("Current todo state:\n- " + "\n- ".join(self.todo_state[-20:]))
        if state_parts:
            result.append(Message("user", "\n\n".join(state_parts)))
        return result + list(self.messages)

    def estimate_chars(self) -> int:
        return len(json.dumps([message.to_dict() for message in self.build_context()], ensure_ascii=False))

    def compact_if_needed(self) -> bool:
        before = self.estimate_chars()
        if before <= self.char_budget or len(self.messages) <= self.keep_recent_messages:
            return False
        cutoff = max(0, len(self.messages) - self.keep_recent_messages)
        while cutoff > 0 and self.messages[cutoff].role == "tool":
            cutoff -= 1
        older = self.messages[:cutoff]
        if not older:
            return False
        snippets = []
        for message in older:
            if message.role == "assistant" and message.tool_calls:
                snippets.append("assistant called: " + ", ".join(call.name for call in message.tool_calls))
            elif message.role == "tool":
                snippets.append("tool result: " + message.content[:500].replace("\n", " "))
            elif message.content:
                snippets.append(f"{message.role}: " + message.content[:500].replace("\n", " "))
        new_summary = "\n".join(snippets)
        if self.history_summary:
            new_summary = self.history_summary + "\n" + new_summary
        self.history_summary = new_summary[-12_000:]
        self.messages = self.messages[cutoff:]
        after = self.estimate_chars()
        self.event_bus.emit("context_compacted", before_chars=before, after_chars=after,
                            removed_messages=len(older))
        return True

