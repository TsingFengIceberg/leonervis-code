from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from leonervis_code.core.actions import ActionPreconditionKind
from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.core.permissions import PermissionAction
from leonervis_code.tools.edit_file import (
    MAX_EDIT_RESULT_BYTES,
    MAX_EDIT_SOURCE_BYTES,
    MAX_EDIT_TEXT_BYTES,
    EditFileOutcome,
    EditFilePreparationError,
    EditFileTool,
)


def request(
    path: str,
    old_text: str,
    new_text: str,
    *,
    tool_use_id: str = "edit-1",
) -> ToolUse:
    return ToolUse(
        tool_use_id,
        "edit_file",
        ToolArguments.from_mapping({"path": path, "old_text": old_text, "new_text": new_text}),
    )


def temporary_files(workspace: Path) -> list[Path]:
    return list(workspace.rglob("*.leonervis-*.tmp"))


def test_prepare_builds_one_exact_candidate_without_side_effects(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = EditFileTool(tmp_path)

    prepared = tool.prepare(request("note.txt", "beta", "gamma"))

    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"
    assert prepared.relative_path == "note.txt"
    assert prepared.content == b"alpha\ngamma\n"
    assert prepared.action == PermissionAction.WORKSPACE_OVERWRITE
    assert prepared.precondition.kind == ActionPreconditionKind.EXPECTED_STATE_SHA256
    assert prepared.precondition.fingerprint is not None
    assert temporary_files(tmp_path) == []


def test_exact_edit_preserves_unicode_and_newline_spelling(tmp_path: Path) -> None:
    target = tmp_path / "unicode.txt"
    target.write_text("狮子\r\nsecond\n", encoding="utf-8", newline="")
    tool = EditFileTool(tmp_path)

    result = tool.execute_detailed(tool.prepare(request("unicode.txt", "狮子\r\n", "LEO\r\n")))

    assert result.outcome == EditFileOutcome.SUCCEEDED
    assert target.read_bytes() == b"LEO\r\nsecond\n"


def test_empty_new_text_performs_one_exact_deletion(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("keep remove me end", encoding="utf-8")
    tool = EditFileTool(tmp_path)

    result = tool.execute_detailed(tool.prepare(request("note.txt", " remove me", "")))

    assert result.result_code == "edited"
    assert result.tool_result.content == (
        '{"bytes_written":8,"operation":"edited","path":"note.txt","replacements":1}\n'
    )
    assert target.read_text(encoding="utf-8") == "keep end"


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "note.txt", "old_text": "a"},
        {"path": "note.txt", "old_text": "a", "new_text": "b", "extra": "x"},
        {"path": 1, "old_text": "a", "new_text": "b"},
        {"path": "note.txt", "old_text": 1, "new_text": "b"},
        {"path": "note.txt", "old_text": "a", "new_text": 1},
    ],
)
def test_prepare_rejects_malformed_arguments(tmp_path: Path, arguments: dict[str, object]) -> None:
    malformed = ToolUse("edit-1", "edit_file", ToolArguments.from_mapping(arguments))

    with pytest.raises(EditFilePreparationError, match="input is malformed"):
        EditFileTool(tmp_path).prepare(malformed)


def test_prepare_rejects_empty_old_text_and_noop(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("alpha", encoding="utf-8")
    tool = EditFileTool(tmp_path)

    with pytest.raises(EditFilePreparationError, match="old_text must not be empty"):
        tool.prepare(request("note.txt", "", "x"))
    with pytest.raises(EditFilePreparationError, match="must change the file"):
        tool.prepare(request("note.txt", "alpha", "alpha"))


@pytest.mark.parametrize("field", ["old_text", "new_text"])
def test_prepare_rejects_each_edit_argument_over_character_bound(
    tmp_path: Path, field: str
) -> None:
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    values = {"old_text": "x", "new_text": "y"}
    values[field] = "a" * (MAX_EDIT_TEXT_BYTES + 1)

    with pytest.raises(EditFilePreparationError, match=f"{field} exceeds"):
        EditFileTool(tmp_path).prepare(request("note.txt", **values))


@pytest.mark.parametrize("field", ["old_text", "new_text"])
def test_prepare_rejects_each_edit_argument_over_utf8_byte_bound(
    tmp_path: Path, field: str
) -> None:
    (tmp_path / "note.txt").write_text("狮", encoding="utf-8")
    values = {"old_text": "狮", "new_text": "虎"}
    values[field] = "狮" * ((MAX_EDIT_TEXT_BYTES // 3) + 1)

    with pytest.raises(EditFilePreparationError, match=f"{field} exceeds"):
        EditFileTool(tmp_path).prepare(request("note.txt", **values))


@pytest.mark.parametrize(
    "path",
    ["", "   ", "/absolute.txt", "../escape.txt", "a/../x", "a//b", "a\\b", "C:/x"],
)
def test_prepare_rejects_nonportable_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(EditFilePreparationError, match="portable workspace-relative"):
        EditFileTool(tmp_path).prepare(request(path, "a", "b"))


def test_prepare_requires_existing_target_and_parent(tmp_path: Path) -> None:
    tool = EditFileTool(tmp_path)

    with pytest.raises(EditFilePreparationError, match="parent directory does not exist"):
        tool.prepare(request("missing/note.txt", "a", "b"))
    with pytest.raises(EditFilePreparationError, match="target must already exist"):
        tool.prepare(request("note.txt", "a", "b"))


def test_prepare_rejects_symlink_directory_binary_and_oversized_sources(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-edit.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    (tmp_path / "directory").mkdir()
    (tmp_path / "binary").write_bytes(b"\xff")
    (tmp_path / "large").write_bytes(b"x" * (MAX_EDIT_SOURCE_BYTES + 1))
    tool = EditFileTool(tmp_path)

    with pytest.raises(EditFilePreparationError, match="must not be a symbolic link"):
        tool.prepare(request("link.txt", "out", "in"))
    with pytest.raises(EditFilePreparationError, match="must be a regular file"):
        tool.prepare(request("directory", "a", "b"))
    with pytest.raises(EditFilePreparationError, match="not valid UTF-8"):
        tool.prepare(request("binary", "a", "b"))
    with pytest.raises(EditFilePreparationError, match="existing file exceeds"):
        tool.prepare(request("large", "x", "y"))
    assert outside.read_text(encoding="utf-8") == "outside"


def test_prepare_requires_exactly_one_match_including_overlaps(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    tool = EditFileTool(tmp_path)

    target.write_text("alpha", encoding="utf-8")
    with pytest.raises(EditFilePreparationError, match="was not found"):
        tool.prepare(request("note.txt", "beta", "x"))

    target.write_text("alpha alpha", encoding="utf-8")
    with pytest.raises(EditFilePreparationError, match="matches more than once"):
        tool.prepare(request("note.txt", "alpha", "x"))

    target.write_text("aaa", encoding="utf-8")
    with pytest.raises(EditFilePreparationError, match="matches more than once"):
        tool.prepare(request("note.txt", "aa", "x"))


def test_prepare_rejects_candidate_over_result_bound(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_bytes(b"b" + (b"a" * (MAX_EDIT_RESULT_BYTES - 1)))

    with pytest.raises(EditFilePreparationError, match="result exceeds"):
        EditFileTool(tmp_path).prepare(request("large.txt", "b", "c" * MAX_EDIT_TEXT_BYTES))


def test_execute_atomically_edits_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    tool = EditFileTool(tmp_path)

    result = tool.execute_detailed(tool.prepare(request("note.txt", "before", "after")))

    assert result.outcome == EditFileOutcome.SUCCEEDED
    assert result.result_code == "edited"
    assert not result.tool_result.is_error
    assert target.read_text(encoding="utf-8") == "after"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert temporary_files(tmp_path) == []


def test_execute_rejects_stale_source_without_losing_external_change(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("approved", encoding="utf-8")
    tool = EditFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "approved", "edited"))
    target.write_text("external", encoding="utf-8")

    result = tool.execute_detailed(prepared)

    assert result.outcome == EditFileOutcome.FAILED
    assert result.result_code == "edit_not_applied"
    assert "conflict" in result.tool_result.content
    assert target.read_text(encoding="utf-8") == "external"
    assert temporary_files(tmp_path) == []


def test_failure_before_replace_keeps_source_and_cleans_temporary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    tool = EditFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "before", "after"))

    def fail_write(_descriptor: int, _content: bytes) -> None:
        raise OSError("injected")

    monkeypatch.setattr("leonervis_code.tools.write_file._write_all", fail_write)
    result = tool.execute_detailed(prepared)

    assert result.outcome == EditFileOutcome.FAILED
    assert result.result_code == "edit_not_applied"
    assert target.read_text(encoding="utf-8") == "before"
    assert temporary_files(tmp_path) == []


def test_directory_fsync_failure_reports_visible_partial_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    tool = EditFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "before", "after"))

    def fail_fsync(_directory: Path) -> None:
        raise OSError("injected")

    monkeypatch.setattr("leonervis_code.tools.write_file._fsync_directory", fail_fsync)
    result = tool.execute_detailed(prepared)

    assert result.outcome == EditFileOutcome.PARTIAL
    assert result.result_code == "edited_durability_unknown"
    assert result.tool_result.is_error
    assert "inspect the workspace and do not retry automatically" in result.tool_result.content
    assert target.read_text(encoding="utf-8") == "after"
    assert temporary_files(tmp_path) == []


def test_refresh_precondition_observes_change_and_deletion(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    tool = EditFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "before", "after"))

    target.write_text("external", encoding="utf-8")
    changed = tool.refresh_precondition(prepared)
    assert changed.kind == ActionPreconditionKind.EXPECTED_STATE_SHA256
    assert changed != prepared.precondition

    os.unlink(target)
    absent = tool.refresh_precondition(prepared)
    assert absent.kind == ActionPreconditionKind.PATH_ABSENT
