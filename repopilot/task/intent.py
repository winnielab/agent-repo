"""Shared task-intent predicates used by planning and review boundaries."""

from __future__ import annotations

import re

from repopilot.task.specification import TaskSpecification


_NEGATED_TEST_CHANGE_PATTERNS = (
    r"\b(?:do not|don't|must not|should not|never|without)\s+"
    r"(?:add|write|create|update|modify|change|extend)\b.{0,30}\b(?:test|tests|coverage)\b",
    r"\b(?:test|tests)\b.{0,30}\b(?:remain unchanged|must not change|must not be modified)\b",
    r"(?:不要|不得|无需|不应|禁止).{0,20}(?:新增|添加|编写|修改|更新|补充).{0,20}(?:测试|覆盖率)",
)

_POSITIVE_TEST_CHANGE_PATTERNS = (
    r"\b(?:add|write|create|update|modify|change|extend)\s+"
    r"(?:(?:a|an|the|new|existing|regression|unit|integration)\s+){0,4}"
    r"(?:test|tests|coverage)\b",
    r"\b(?:test|tests|coverage)\b.{0,24}\b"
    r"(?:must|should|need(?:s)? to|requested to)\s+(?:be\s+)?"
    r"(?:added|written|created|updated|modified|changed|extended)\b",
    r"(?:新增|添加|编写|修改|更新|补充).{0,20}(?:测试|覆盖率)",
    r"(?:测试|覆盖率).{0,20}(?:需要|要求|应当|必须).{0,10}(?:新增|添加|编写|修改|更新|补充)",
)
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|testing)(?:/|$)|(?:^|/)(?:test_[^/]+|[^/]+_test)\.[A-Za-z0-9]+$",
    re.IGNORECASE,
)


def is_test_path(path: str) -> bool:
    """Classify conventional test artifacts without repository-specific rules."""
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


def requests_test_changes(specification: TaskSpecification) -> bool:
    """Return true only for an affirmative request to change test artifacts.

    Phrases such as "fix the implementation so tests pass" and "do not modify
    tests" describe verification or a negative constraint, not test-writing
    work. Keeping this predicate shared prevents Planner and Reviewer from
    applying contradictory interpretations to the same specification.
    """
    text = "\n".join([
        specification.title,
        specification.description,
        *specification.constraints,
    ]).casefold()
    for pattern in _NEGATED_TEST_CHANGE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _POSITIVE_TEST_CHANGE_PATTERNS)
