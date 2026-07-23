"""Deterministic bounded literal content search for Foundation 1D."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from leonervis_code.core.contracts import ToolResult, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.tools.file_selector import (
    FileSelectionFailure,
    SelectorFailureKind,
    select_files,
)

GREP_TOOL_NAME = "grep"
MAX_GREP_QUERY_CHARACTERS = 4096
MAX_GREP_QUERY_BYTES = 4096
MAX_GREP_CANDIDATE_FILES = 1_000
MAX_GREP_FILE_BYTES = 1024 * 1024
MAX_GREP_AGGREGATE_BYTES = 16 * 1024 * 1024
MAX_GREP_MATCHES = 200
MAX_GREP_OUTPUT_BYTES = 32 * 1024
GREP_TRUNCATION_SENTINEL = '{"truncated":true}\n'


class _GrepFailure(RuntimeError):
    """One stable model-visible content-search failure."""


def grep_model_definition() -> dict[str, object]:
    """Return a fresh provider-neutral definition of bounded literal grep."""
    return {
        "name": GREP_TOOL_NAME,
        "description": (
            "Search for one case-sensitive literal string in regular UTF-8 workspace files "
            "selected by one portable include pattern. Use this read-only bounded tool when "
            "the content is known but its file location is not. It never returns or traverses "
            "symbolic links and returns deterministic JSON Lines with path, line, and text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Case-sensitive literal text to find within one logical line.",
                },
                "include": {
                    "type": "string",
                    "description": (
                        "Portable workspace-relative '/'-separated regular-file glob pattern."
                    ),
                },
            },
            "required": ["query", "include"],
            "additionalProperties": False,
        },
    }


def grep_tool_snapshot() -> CanonicalToolDefinition:
    """Freeze canonical grep definition for effective-context identity."""
    return CanonicalToolDefinition.from_mapping(grep_model_definition())


class GrepTool:
    """Search bounded UTF-8 regular files without following links or leaving a workspace."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        if not self._workspace.is_dir():
            raise ValueError("workspace must be an existing directory")

    def execute(self, request: ToolUse) -> ToolResult:
        """Return deterministic JSONL matching lines or one safe bounded failure."""
        try:
            arguments = request.arguments.as_mapping()
            if set(arguments) != {"include", "query"}:
                raise _GrepFailure("grep input is malformed")
            query = arguments["query"]
            include = arguments["include"]
            if not isinstance(query, str) or not isinstance(include, str):
                raise _GrepFailure("grep input is malformed")
            _validate_query(query)
            try:
                candidates = select_files(
                    self._workspace,
                    include,
                    max_files=MAX_GREP_CANDIDATE_FILES,
                )
            except FileSelectionFailure as error:
                raise _GrepFailure(_selector_message(error.kind)) from None
            content, truncated = self._search(query, candidates)
        except (AttributeError, ValueError):
            return self._error(request, "grep input is malformed")
        except _GrepFailure as error:
            return self._error(request, str(error))
        return ToolResult(
            tool_use_id=request.tool_use_id,
            content=content,
            truncated=truncated,
        )

    def _search(self, query: str, candidates) -> tuple[str, bool]:
        output: list[str] = []
        output_bytes = 0
        aggregate_bytes = 0
        match_count = 0
        sentinel_bytes = len(GREP_TRUNCATION_SENTINEL.encode("utf-8"))

        for candidate in candidates:
            data = _read_candidate(candidate.path)
            aggregate_bytes += len(data)
            if aggregate_bytes > MAX_GREP_AGGREGATE_BYTES:
                raise _GrepFailure("grep aggregate read limit reached; use a narrower include")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise _GrepFailure("grep encountered a file that is not valid UTF-8") from None
            if "\x00" in text:
                raise _GrepFailure("grep encountered a file containing NUL")

            for line_number, line in enumerate(_logical_lines(text), start=1):
                if query not in line:
                    continue
                record = _match_record(candidate.relative_path, line_number, line)
                record_bytes = len(record.encode("utf-8"))
                if record_bytes + sentinel_bytes > MAX_GREP_OUTPUT_BYTES:
                    raise _GrepFailure(
                        "grep matching line exceeds the output limit; use read_file for that file"
                    )
                match_count += 1
                if (
                    match_count > MAX_GREP_MATCHES
                    or output_bytes + record_bytes > MAX_GREP_OUTPUT_BYTES
                ):
                    output.append(GREP_TRUNCATION_SENTINEL)
                    return "".join(output), True
                if output_bytes + record_bytes + sentinel_bytes > MAX_GREP_OUTPUT_BYTES:
                    output.append(GREP_TRUNCATION_SENTINEL)
                    return "".join(output), True
                output.append(record)
                output_bytes += record_bytes

        return "".join(output), False

    @staticmethod
    def _error(request: ToolUse, content: str) -> ToolResult:
        return ToolResult(tool_use_id=request.tool_use_id, content=content, is_error=True)


def _validate_query(query: str) -> None:
    if not query:
        raise _GrepFailure("grep query must not be empty")
    if any(character in query for character in ("\x00", "\r", "\n")):
        raise _GrepFailure("grep query must be one line without NUL")
    try:
        encoded = query.encode("utf-8")
    except UnicodeEncodeError:
        raise _GrepFailure("grep query must be valid UTF-8") from None
    if len(query) > MAX_GREP_QUERY_CHARACTERS or len(encoded) > MAX_GREP_QUERY_BYTES:
        raise _GrepFailure("grep query exceeds the supported size")


def _read_candidate(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
    except (FileNotFoundError, PermissionError):
        raise _GrepFailure("grep encountered an unreadable file") from None
    except OSError:
        raise _GrepFailure("grep could not read a selected file") from None

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise _GrepFailure("grep selected file changed before it could be read")
        if opened.st_size > MAX_GREP_FILE_BYTES:
            raise _GrepFailure("grep selected file exceeds the per-file limit")
        chunks: list[bytes] = []
        remaining = MAX_GREP_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_GREP_FILE_BYTES:
            raise _GrepFailure("grep selected file exceeds the per-file limit")
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
            raise _GrepFailure("grep selected file changed before it could be read")
        return data
    except _GrepFailure:
        raise
    except OSError:
        raise _GrepFailure("grep could not read a selected file") from None
    finally:
        os.close(descriptor)


def _logical_lines(text: str) -> list[str]:
    if not text:
        return []
    lines: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in {"\r", "\n"}:
            index += 1
            continue
        lines.append(text[start:index])
        if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 1
        index += 1
        start = index
    if start < len(text):
        lines.append(text[start:])
    return lines


def _match_record(path: str, line: int, text: str) -> str:
    return (
        json.dumps(
            {"path": path, "line": line, "text": text},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _selector_message(kind: SelectorFailureKind) -> str:
    messages = {
        SelectorFailureKind.BLANK_PATTERN: "grep include must not be blank",
        SelectorFailureKind.INVALID_COMPONENT: (
            "grep include contains an unsupported path component"
        ),
        SelectorFailureKind.OVERSIZED_PATTERN: "grep include exceeds the supported size",
        SelectorFailureKind.NONPORTABLE_PATTERN: (
            "grep include must be workspace-relative and use '/' separators"
        ),
        SelectorFailureKind.DIRECTORY_LIMIT: (
            "grep directory limit reached; use a narrower include"
        ),
        SelectorFailureKind.UNREADABLE_DIRECTORY: ("grep encountered an unreadable directory"),
        SelectorFailureKind.SCAN_FAILED: "grep could not scan the workspace",
        SelectorFailureKind.INVALID_UTF8_PATH: ("grep encountered a path that is not valid UTF-8"),
        SelectorFailureKind.ENTRY_LIMIT: (
            "grep traversal entry limit reached; use a narrower include"
        ),
        SelectorFailureKind.DEPTH_LIMIT: "grep depth limit reached; use a narrower include",
        SelectorFailureKind.FILE_LIMIT: (
            "grep candidate file limit reached; use a narrower include"
        ),
    }
    return messages[kind]
