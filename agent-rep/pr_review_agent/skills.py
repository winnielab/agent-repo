"""Modularized skills component of the PR review agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable



class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict[str, str]] = {}
        if not skills_dir.exists():
            return
        for path in sorted(skills_dir.rglob("SKILL.md")):
            text = path.read_text(encoding="utf-8")
            meta, body = self._parse(text)
            name = meta.get("name", path.parent.name)
            self.skills[name] = {"description": meta.get("description", ""), "body": body}

    def _parse(self, text: str) -> tuple[dict[str, str], str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        return meta, match.group(2).strip()

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills)"
        return "\n".join(f"- {name}: {data['description']}" for name, data in self.skills.items())

    def load(self, name: str) -> str:
        data = self.skills.get(name)
        if not data:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills)}"
        return f"<skill name=\"{name}\">\n{data['body']}\n</skill>"
