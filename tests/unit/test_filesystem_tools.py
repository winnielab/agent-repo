import asyncio

import pytest

from repopilot.core.models import ToolCall
from repopilot.execution.environment import LocalEnvironment
from repopilot.observability.events import EventBus
from repopilot.tools.base import ToolContext
from repopilot.tools.editing import DeleteFileTool, EditFileTool, WriteFileTool
from repopilot.tools.filesystem import ReadFileTool
from repopilot.tools.registry import ToolRegistry
from repopilot.tools.search import SearchCodeTool


def context(path):
    return ToolContext(path, LocalEnvironment(path), "run", EventBus("run"))


def test_read_search_write_and_exact_edit(tmp_path) -> None:
    (tmp_path / "a.py").write_text("first\nneedle\nlast\n")
    registry = ToolRegistry([ReadFileTool(), SearchCodeTool(), WriteFileTool(), EditFileTool()])
    ctx = context(tmp_path)
    read = asyncio.run(registry.execute(ToolCall("1", "read_file", {"path": "a.py", "offset": 2, "limit": 1}), ctx))
    search = asyncio.run(registry.execute(ToolCall("2", "search_code", {"query": "needle"}), ctx))
    edit = asyncio.run(registry.execute(ToolCall("3", "edit_file", {"path": "a.py", "old_text": "needle", "new_text": "fixed"}), ctx))
    write = asyncio.run(registry.execute(ToolCall("4", "write_file", {"path": "sub/b.py", "content": "ok\n"}), ctx))
    assert "2\tneedle" in read.content
    assert ctx.values["read_files"] == ["a.py"]
    assert "a.py:2:needle" in search.content
    assert edit.ok and write.ok
    assert "fixed" in (tmp_path / "a.py").read_text()


def test_path_and_edit_conflicts_do_not_modify(tmp_path) -> None:
    (tmp_path / "a.py").write_text("same\nsame\n")
    registry = ToolRegistry([ReadFileTool(), EditFileTool()])
    ctx = context(tmp_path)
    escape = asyncio.run(registry.execute(ToolCall("1", "read_file", {"path": "../outside"}), ctx))
    conflict = asyncio.run(registry.execute(ToolCall("2", "edit_file", {"path": "a.py", "old_text": "same", "new_text": "x"}), ctx))
    assert not escape.ok
    assert conflict.error_code == "edit_conflict"
    assert (tmp_path / "a.py").read_text() == "same\nsame\n"


def test_symlink_escape_is_rejected(tmp_path) -> None:
    outside = tmp_path.parent / "outside-repopilot-test.txt"
    outside.write_text("secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    registry = ToolRegistry([ReadFileTool()])
    result = asyncio.run(registry.execute(ToolCall("1", "read_file", {"path": "link.txt"}), context(tmp_path)))
    assert not result.ok
    assert "escapes workspace" in result.content


def test_delete_file_removes_only_regular_workspace_file(tmp_path) -> None:
    (tmp_path / "temporary.py").write_text("print('temporary')\n")
    (tmp_path / "folder").mkdir()
    registry = ToolRegistry([DeleteFileTool()])
    deleted = asyncio.run(registry.execute(ToolCall("1", "delete_file", {"path": "temporary.py"}), context(tmp_path)))
    refused = asyncio.run(registry.execute(ToolCall("2", "delete_file", {"path": "folder"}), context(tmp_path)))
    assert deleted.ok and not (tmp_path / "temporary.py").exists()
    assert not refused.ok and (tmp_path / "folder").is_dir()


def test_editing_tools_enforce_task_test_change_contract(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    test_file = tmp_path / "tests" / "test_behavior.py"
    test_file.write_text("assert behavior()\n", encoding="utf-8")
    registry = ToolRegistry([WriteFileTool(), EditFileTool(), DeleteFileTool()])
    ctx = context(tmp_path)
    ctx.values["allow_test_changes"] = False

    edited = asyncio.run(registry.execute(ToolCall("1", "edit_file", {
        "path": "tests/test_behavior.py",
        "old_text": "assert behavior()",
        "new_text": "assert True",
    }), ctx))
    written = asyncio.run(registry.execute(ToolCall("2", "write_file", {
        "path": "test_new_behavior.py", "content": "assert True\n",
    }), ctx))
    deleted = asyncio.run(registry.execute(ToolCall("3", "delete_file", {
        "path": "tests/test_behavior.py",
    }), ctx))

    assert {edited.error_code, written.error_code, deleted.error_code} == {
        "test_change_not_allowed",
    }
    assert test_file.read_text(encoding="utf-8") == "assert behavior()\n"
    assert not (tmp_path / "test_new_behavior.py").exists()
