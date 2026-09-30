#!/usr/bin/env python3
"""Compatibility entry point for the former single-file agent API."""

from pr_review_agent import *  # noqa: F403
from pr_review_agent import main


if __name__ == "__main__":
    raise SystemExit(main())
