"""Validated JSON extraction for model-produced orchestration data."""

from __future__ import annotations

import json
import re
from typing import Any, Dict


def parse_json_object(text: str) -> Dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end >= start:
            candidate = candidate[start:end + 1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("structured response must be a JSON object")
    return value

