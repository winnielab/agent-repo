"""Small synchronous event bus with durable JSONL traces."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

from repopilot.core.models import json_safe

Subscriber = Callable[[Dict[str, Any]], None]


@dataclass
class EventBus:
    run_id: str
    subscribers: List[Subscriber] = field(default_factory=list)

    def subscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.append(subscriber)

    def emit(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        timestamp = datetime.now(timezone.utc).isoformat()
        safe_payload = json_safe(payload)
        event = {
            "timestamp": timestamp,
            "run_id": self.run_id,
            "event_type": event_type,
            "payload": safe_payload,
            # Stable aliases consumed by the Stage 1 benchmark.
            "type": event_type,
            "data": safe_payload,
        }
        for subscriber in list(self.subscribers):
            subscriber(event)
        return event


class MemorySubscriber:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def __call__(self, event: Dict[str, Any]) -> None:
        self.events.append(event)


class JSONLSubscriber:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, event: Dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

