"""Composable PR review agent package.

The imports below form the package's public assembly surface.  Existing users
can keep importing the familiar names while new code can depend on the smaller
component modules directly.
"""

import urllib.error
import urllib.request

from .agent import ReviewAgent
from .cli import build_parser, main
from .domain import (
    CouncilFinding,
    CouncilMessage,
    EvidenceItem,
    EvidenceStore,
    Finding,
    FindingCollector,
    FindingLifecycle,
    MessageBus,
    TodoManager,
    Transcript,
)
from .judge import JudgeRunner
from .knowledge import CompanyKnowledgeBase, CompanyKnowledgeChunk
from .llm import AliyunDashScopeClient, LLMClient, OpenAICompatibleClient
from .orchestration import (
    AgenticReviewLoop,
    CriticReviewer,
    DebateCouncilLoop,
    ReviewAgentMember,
    ReviewCouncil,
)
from .reporting import CouncilReportWriter, ReportWriter, ReportWriterAgent, zh_text
from .reviewers import SpecialtyReviewers
from .settings import (
    CATEGORIES,
    COMPANY_KNOWLEDGE_INDEX_NAME,
    DASHSCOPE_BASE_URL,
    FINDINGS_NAME,
    JUDGE_INPUT_NAME,
    JUDGE_NAME,
    JUDGE_REPORT_NAME,
    JUDGE_TRANSCRIPT_NAME,
    LLM_PROVIDERS,
    MAX_CMD_OUTPUT,
    MAX_COMPANY_CONTEXT_CHARS,
    MAX_DIFF_CHARS,
    MAX_SKILL_CHARS,
    OUTPUT_DIR_NAME,
    PRIVATE_KEY_RE,
    REPORT_NAME,
    REPO_ROOT,
    SECRET_RE,
    SEVERITIES,
    TOOLS,
    TRANSCRIPT_NAME,
    VERDICTS,
)
from .skills import SkillLoader
from .tools import ReviewTools
from .utils import (
    git_range,
    is_git_repo,
    is_source_file,
    is_test_file,
    iter_added_lines,
    iter_review_files,
    load_env_file,
    now,
    parse_numstat,
    run_git,
    safe_repo_path,
    synthetic_changed_files,
    synthetic_diff,
    truncate,
)

__all__ = [name for name in globals() if not name.startswith("_")]
