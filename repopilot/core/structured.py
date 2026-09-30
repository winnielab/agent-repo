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
        start = candidate.find("{")
        if start >= 0:
            candidate = candidate[start:]
    # Model providers occasionally append a second object or prose note even
    # when asked for JSON only. Decode exactly the first complete object rather
    # than expanding from the first opening brace to the final closing brace.
    # Caller-level schema validation remains the semantic boundary.
    value, _end = json.JSONDecoder().raw_decode(candidate)
    if not isinstance(value, dict):
        raise ValueError("structured response must be a JSON object")
    return value
