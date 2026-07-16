from __future__ import annotations

import pytest

from leonervis_code.core.contracts import ToolUse
from leonervis_code.tools.read_file import MAX_CONTENT_BYTES, TRUNCATION_MARKER, ReadFileTool


def request(path: str = "README.md") -> ToolUse:
    return ToolUse(tool_use_id="read-1", name="read_file", path=path)


def test_read_file_returns_nested_workspace_text_without_writing(tmp_path) -> None:
    nested = tmp_path / "docs"
    nested.mkdir()
    file = nested / "README.md"
    file.write_text("project notes\n", encoding="utf-8")

    result = ReadFileTool(tmp_path).execute(request("docs/README.md"))

    assert result.content == "project notes\n"
    assert not result.is_error
    assert not result.truncated
    assert file.read_text(encoding="utf-8") == "project notes\n"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing.txt", "does not exist"),
        (".", "not a file"),
        ("../outside.txt", "escapes the workspace"),
    ],
)
def test_read_file_rejects_invalid_workspace_paths(tmp_path, path, message) -> None:
    (tmp_path.parent / "outside.txt").write_text("outside", encoding="utf-8")

    result = ReadFileTool(tmp_path).execute(request(path))

    assert result.is_error
    assert message in result.content


def test_read_file_rejects_absolute_and_symlink_escape_paths(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)
    (tmp_path / "linked").symlink_to(tmp_path.parent, target_is_directory=True)
    tool = ReadFileTool(tmp_path)

    absolute = tool.execute(request(str(outside)))
    final_symlink = tool.execute(request("escape.txt"))
    intermediate_symlink = tool.execute(request("linked/outside.txt"))

    assert "must be relative" in absolute.content
    assert "escapes the workspace" in final_symlink.content
    assert "escapes the workspace" in intermediate_symlink.content


def test_read_file_rejects_invalid_utf8(tmp_path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"\xff\xfe")

    result = ReadFileTool(tmp_path).execute(request("binary.dat"))

    assert result.is_error
    assert result.content == "read_file content is not valid UTF-8"


def test_read_file_bounds_content_at_32_kib_with_a_utf8_safe_marker(tmp_path) -> None:
    exact = "a" * MAX_CONTENT_BYTES
    (tmp_path / "exact.txt").write_text(exact, encoding="utf-8")
    marker_size = len(TRUNCATION_MARKER.encode("utf-8"))
    oversized = ("a" * (MAX_CONTENT_BYTES - marker_size - 1)) + "你" + ("z" * marker_size)
    (tmp_path / "oversized.txt").write_text(oversized, encoding="utf-8")
    tool = ReadFileTool(tmp_path)

    exact_result = tool.execute(request("exact.txt"))
    oversized_result = tool.execute(request("oversized.txt"))

    assert exact_result.content == exact
    assert not exact_result.truncated
    assert oversized_result.truncated
    assert oversized_result.content.endswith(TRUNCATION_MARKER)
    assert len(oversized_result.content.encode("utf-8")) <= MAX_CONTENT_BYTES
    assert not oversized_result.is_error


def test_read_file_rejects_invalid_utf8_before_the_truncation_boundary(tmp_path) -> None:
    (tmp_path / "invalid-large.dat").write_bytes((b"a" * MAX_CONTENT_BYTES) + b"\xff")

    result = ReadFileTool(tmp_path).execute(request("invalid-large.dat"))

    assert result.is_error
    assert result.content == "read_file content is not valid UTF-8"
