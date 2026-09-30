"""Path containment helpers that reject traversal and symlink escapes."""

from pathlib import Path

from repopilot.core.errors import PathBoundaryError


def safe_workspace_path(workspace: Path, requested: str, must_exist: bool = False) -> Path:
    root = Path(workspace).resolve()
    candidate = (root / requested).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathBoundaryError(f"path escapes workspace: {requested}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(requested)
    return candidate


def safe_cwd(workspace: Path, cwd: Path) -> Path:
    root = Path(workspace).resolve()
    candidate = Path(cwd).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathBoundaryError(f"cwd escapes workspace: {cwd}")
    if not candidate.is_dir():
        raise NotADirectoryError(str(cwd))
    return candidate

