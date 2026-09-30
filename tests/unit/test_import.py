import subprocess
import sys


def test_import_repopilot_does_not_import_pr_agent() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys,repopilot; assert 'pr_review_agent' not in sys.modules"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
