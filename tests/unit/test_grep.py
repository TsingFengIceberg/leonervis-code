from __future__ import annotations

import json
import os

import pytest

from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.tools import grep as grep_module
from leonervis_code.tools.grep import (
    GREP_TRUNCATION_SENTINEL,
    MAX_GREP_MATCHES,
    GrepTool,
)


def request(query: str, include: str = "**/*.txt") -> ToolUse:
    return ToolUse(
        "grep-1",
        "grep",
        ToolArguments.from_mapping({"query": query, "include": include}),
    )


def records(result) -> list[dict[str, object]]:
    return [json.loads(line) for line in result.content.splitlines()]


def test_grep_matches_literal_lines_in_stable_path_and_line_order(tmp_path) -> None:
    (tmp_path / "z.txt").write_text("Needle twice Needle\nneedle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("zero\nNeedle\n", encoding="utf-8")

    result = GrepTool(tmp_path).execute(request("Needle"))

    assert records(result) == [
        {"path": "a.txt", "line": 2, "text": "Needle"},
        {"path": "z.txt", "line": 1, "text": "Needle twice Needle"},
    ]
    assert not result.is_error and not result.truncated


def test_grep_handles_newline_forms_unterminated_lines_and_json_escaping(tmp_path) -> None:
    (tmp_path / "lines.txt").write_bytes('q "one"\rsecond q\\tab\t\r\nq 三\nfour q'.encode("utf-8"))

    result = GrepTool(tmp_path).execute(request("q"))

    assert records(result) == [
        {"path": "lines.txt", "line": 1, "text": 'q "one"'},
        {"path": "lines.txt", "line": 2, "text": "second q\\tab\t"},
        {"path": "lines.txt", "line": 3, "text": "q 三"},
        {"path": "lines.txt", "line": 4, "text": "four q"},
    ]


def test_grep_no_match_is_successful_empty_content(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("other", encoding="utf-8")

    result = GrepTool(tmp_path).execute(request("missing"))

    assert result.content == ""
    assert not result.is_error and not result.truncated


def test_grep_uses_glob_hidden_and_symlink_semantics(tmp_path) -> None:
    (tmp_path / "visible.txt").write_text("hit", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("hit", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("visible.txt\n", encoding="utf-8")
    directory = tmp_path / "real"
    directory.mkdir()
    (directory / "inside.txt").write_text("hit", encoding="utf-8")
    (tmp_path / "linked-dir").symlink_to(directory, target_is_directory=True)
    (tmp_path / "linked.txt").symlink_to(directory / "inside.txt")

    normal = GrepTool(tmp_path).execute(request("hit"))
    hidden = GrepTool(tmp_path).execute(request("hit", ".*.txt"))

    assert [item["path"] for item in records(normal)] == ["real/inside.txt", "visible.txt"]
    assert [item["path"] for item in records(hidden)] == [".hidden.txt"]


@pytest.mark.parametrize("query", ["", "bad\x00", "bad\r", "bad\n", "界" * 2049])
def test_grep_rejects_invalid_queries(tmp_path, query) -> None:
    result = GrepTool(tmp_path).execute(request(query))

    assert result.is_error
    assert result.content.startswith("grep query")


@pytest.mark.parametrize("include", ["", "/tmp/*.txt", "../*.txt", "a//*.txt", "a/**x"])
def test_grep_rejects_invalid_includes(tmp_path, include) -> None:
    result = GrepTool(tmp_path).execute(request("x", include))

    assert result.is_error
    assert result.content.startswith("grep include")


def test_grep_fails_closed_on_non_utf8_nul_and_oversized_files(tmp_path, monkeypatch) -> None:
    (tmp_path / "bad.txt").write_bytes(b"hit\xff")
    invalid = GrepTool(tmp_path).execute(request("hit"))
    assert invalid.is_error and "valid UTF-8" in invalid.content

    (tmp_path / "bad.txt").write_bytes(b"hit\x00")
    nul = GrepTool(tmp_path).execute(request("hit"))
    assert nul.is_error and "NUL" in nul.content

    monkeypatch.setattr(grep_module, "MAX_GREP_FILE_BYTES", 2)
    oversized = GrepTool(tmp_path).execute(request("hit"))
    assert oversized.is_error and "per-file" in oversized.content


def test_grep_bounds_candidate_and_aggregate_bytes(tmp_path, monkeypatch) -> None:
    for number in range(3):
        (tmp_path / f"{number}.txt").write_text("hit", encoding="utf-8")
    monkeypatch.setattr(grep_module, "MAX_GREP_CANDIDATE_FILES", 2)
    candidates = GrepTool(tmp_path).execute(request("hit"))
    assert candidates.is_error and "candidate file limit" in candidates.content

    monkeypatch.setattr(grep_module, "MAX_GREP_CANDIDATE_FILES", 3)
    monkeypatch.setattr(grep_module, "MAX_GREP_AGGREGATE_BYTES", 8)
    aggregate = GrepTool(tmp_path).execute(request("hit"))
    assert aggregate.is_error and "aggregate read limit" in aggregate.content


def test_grep_truncates_complete_json_records_at_match_boundary(tmp_path) -> None:
    (tmp_path / "many.txt").write_text(
        "".join(f"hit {number}\n" for number in range(MAX_GREP_MATCHES + 1)),
        encoding="utf-8",
    )

    result = GrepTool(tmp_path).execute(request("hit"))

    assert result.truncated and not result.is_error
    assert result.content.endswith(GREP_TRUNCATION_SENTINEL)
    decoded = records(result)
    assert len(decoded) == MAX_GREP_MATCHES + 1
    assert decoded[-1] == {"truncated": True}


def test_grep_bounds_output_without_splitting_utf8_or_json(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("hit 一\nhit 二\n", encoding="utf-8")
    monkeypatch.setattr(grep_module, "MAX_GREP_OUTPUT_BYTES", 75)

    result = GrepTool(tmp_path).execute(request("hit"))

    assert result.truncated
    assert len(result.content.encode("utf-8")) <= 75
    records(result)


def test_grep_errors_when_one_matching_record_exceeds_output_limit(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("hit " + "x" * 100, encoding="utf-8")
    monkeypatch.setattr(grep_module, "MAX_GREP_OUTPUT_BYTES", 64)

    result = GrepTool(tmp_path).execute(request("hit"))

    assert result.is_error
    assert "matching line exceeds" in result.content
    assert result.content.find(str(tmp_path)) < 0


def test_grep_redacts_file_read_errors(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("hit", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise PermissionError("secret raw path")

    monkeypatch.setattr(os, "open", fail)
    result = GrepTool(tmp_path).execute(request("hit"))

    assert result.is_error
    assert result.content == "grep encountered an unreadable file"
    assert "secret" not in result.content
