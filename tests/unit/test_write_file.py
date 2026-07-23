from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from leonervis_code.core.actions import ActionPreconditionKind
from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.core.permissions import PermissionAction
from leonervis_code.tools.write_file import (
    MAX_OVERWRITE_SOURCE_BYTES,
    MAX_WRITE_CONTENT_BYTES,
    WriteFileOutcome,
    WriteFilePreparationError,
    WriteFileTool,
)


def request(path: str, content: str = "new text\n", *, tool_use_id: str = "write-1") -> ToolUse:
    return ToolUse(
        tool_use_id,
        "write_file",
        ToolArguments.from_mapping({"path": path, "content": content}),
    )


def temporary_files(workspace: Path) -> list[Path]:
    return list(workspace.rglob("*.leonervis-*.tmp"))


def test_prepare_classifies_absent_target_as_create_and_execute_is_deterministic(
    tmp_path: Path,
) -> None:
    tool = WriteFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "hello\n"))

    assert prepared.relative_path == "note.txt"
    assert prepared.content == b"hello\n"
    assert prepared.action == PermissionAction.WORKSPACE_CREATE
    assert prepared.precondition.kind == ActionPreconditionKind.PATH_ABSENT

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.SUCCEEDED
    assert result.result_code == "created"
    assert result.tool_result.content == (
        '{"bytes_written":6,"operation":"created","path":"note.txt"}\n'
    )
    assert not result.tool_result.is_error
    assert (tmp_path / "note.txt").read_bytes() == b"hello\n"
    assert temporary_files(tmp_path) == []


def test_empty_content_is_a_valid_create(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)

    result = tool.execute_detailed(tool.prepare(request("empty.txt", "")))

    assert result.outcome == WriteFileOutcome.SUCCEEDED
    assert result.tool_result.content == (
        '{"bytes_written":0,"operation":"created","path":"empty.txt"}\n'
    )
    assert (tmp_path / "empty.txt").read_bytes() == b""


def test_prepare_classifies_existing_utf8_file_as_exact_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o640)
    tool = WriteFileTool(tmp_path)

    prepared = tool.prepare(request("note.txt", "replacement\n"))

    assert prepared.action == PermissionAction.WORKSPACE_OVERWRITE
    assert prepared.precondition.kind == ActionPreconditionKind.EXPECTED_STATE_SHA256
    assert prepared.precondition.fingerprint is not None

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.SUCCEEDED
    assert result.result_code == "overwritten"
    assert target.read_text(encoding="utf-8") == "replacement\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert temporary_files(tmp_path) == []


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "/absolute.txt",
        "../escape.txt",
        "a/../escape.txt",
        "a//b.txt",
        "a/",
        "a\\b.txt",
        "C:/x.txt",
    ],
)
def test_prepare_rejects_nonportable_or_escaping_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(WriteFilePreparationError, match="portable workspace-relative"):
        WriteFileTool(tmp_path).prepare(request(path))


def test_prepare_requires_existing_real_parent_directories(tmp_path: Path) -> None:
    file_parent = tmp_path / "file-parent"
    file_parent.write_text("x", encoding="utf-8")
    tool = WriteFileTool(tmp_path)

    with pytest.raises(WriteFilePreparationError, match="parent directory does not exist"):
        tool.prepare(request("missing/child.txt"))
    with pytest.raises(WriteFilePreparationError, match="parent path is not a directory"):
        tool.prepare(request("file-parent/child.txt"))

    assert not (tmp_path / "missing").exists()


def test_prepare_rejects_final_intermediate_internal_external_and_broken_symlinks(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-write-target.txt"
    outside.write_text("outside", encoding="utf-8")
    real = tmp_path / "real"
    real.mkdir()
    (real / "inside.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "final-internal").symlink_to(real / "inside.txt")
    (tmp_path / "final-external").symlink_to(outside)
    (tmp_path / "broken").symlink_to(tmp_path / "missing")
    (tmp_path / "dir-link").symlink_to(real, target_is_directory=True)
    tool = WriteFileTool(tmp_path)

    for path in ["final-internal", "final-external", "broken"]:
        with pytest.raises(WriteFilePreparationError, match="must not be a symbolic link"):
            tool.prepare(request(path))
    with pytest.raises(WriteFilePreparationError, match="contains a symbolic link"):
        tool.prepare(request("dir-link/new.txt"))

    assert outside.read_text(encoding="utf-8") == "outside"
    assert (real / "inside.txt").read_text(encoding="utf-8") == "inside"


def test_prepare_rejects_directory_non_utf8_and_oversized_existing_target(tmp_path: Path) -> None:
    (tmp_path / "directory").mkdir()
    (tmp_path / "binary.dat").write_bytes(b"\xff")
    (tmp_path / "large.txt").write_bytes(b"a" * (MAX_OVERWRITE_SOURCE_BYTES + 1))
    tool = WriteFileTool(tmp_path)

    with pytest.raises(WriteFilePreparationError, match="regular file"):
        tool.prepare(request("directory"))
    with pytest.raises(WriteFilePreparationError, match="not valid UTF-8"):
        tool.prepare(request("binary.dat"))
    with pytest.raises(WriteFilePreparationError, match="existing file exceeds"):
        tool.prepare(request("large.txt"))


@pytest.mark.parametrize(
    "content",
    ["a" * (MAX_WRITE_CONTENT_BYTES + 1), "你" * ((MAX_WRITE_CONTENT_BYTES // 3) + 1)],
)
def test_prepare_rejects_content_over_character_or_utf8_byte_limit(
    tmp_path: Path, content: str
) -> None:
    with pytest.raises(WriteFilePreparationError, match="content exceeds"):
        WriteFileTool(tmp_path).prepare(request("large.txt", content))


def test_create_conflict_does_not_replace_target_that_appears_after_prepare(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "model\n"))
    (tmp_path / "note.txt").write_text("external\n", encoding="utf-8")

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.FAILED
    assert result.result_code == "write_not_applied"
    assert "no longer absent" in result.tool_result.content
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "external\n"
    assert temporary_files(tmp_path) == []


def test_overwrite_conflict_preserves_target_changed_after_prepare(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    tool = WriteFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "model\n"))
    target.write_text("external\n", encoding="utf-8")

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.FAILED
    assert "no longer matches" in result.tool_result.content
    assert target.read_text(encoding="utf-8") == "external\n"
    assert temporary_files(tmp_path) == []


def test_create_failure_before_install_leaves_no_target_or_temporary_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = WriteFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "model\n"))

    def fail_write(_descriptor: int, _content: bytes) -> None:
        raise OSError("injected")

    monkeypatch.setattr("leonervis_code.tools.write_file._write_all", fail_write)

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.FAILED
    assert not (tmp_path / "note.txt").exists()
    assert temporary_files(tmp_path) == []


def test_overwrite_failure_before_replace_preserves_original_and_removes_temporary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    tool = WriteFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "model\n"))

    def fail_replace(_source, _target) -> None:
        raise OSError("injected")

    monkeypatch.setattr(os, "replace", fail_replace)

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.FAILED
    assert target.read_text(encoding="utf-8") == "old\n"
    assert temporary_files(tmp_path) == []


def test_create_directory_fsync_failure_reports_visible_durability_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = WriteFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "model\n"))

    def fail_fsync(_directory: Path) -> None:
        raise OSError("injected")

    monkeypatch.setattr("leonervis_code.tools.write_file._fsync_directory", fail_fsync)

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.PARTIAL
    assert result.result_code == "created_durability_unknown"
    assert result.tool_result.is_error
    assert "do not retry automatically" in result.tool_result.content
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "model\n"
    assert temporary_files(tmp_path) == []


def test_overwrite_directory_fsync_failure_reports_visible_durability_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    tool = WriteFileTool(tmp_path)
    prepared = tool.prepare(request("note.txt", "model\n"))

    def fail_fsync(_directory: Path) -> None:
        raise OSError("injected")

    monkeypatch.setattr("leonervis_code.tools.write_file._fsync_directory", fail_fsync)

    result = tool.execute_detailed(prepared)

    assert result.outcome == WriteFileOutcome.PARTIAL
    assert result.result_code == "overwritten_durability_unknown"
    assert result.tool_result.is_error
    assert target.read_text(encoding="utf-8") == "model\n"
    assert temporary_files(tmp_path) == []
