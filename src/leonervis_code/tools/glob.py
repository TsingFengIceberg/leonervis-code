"""Deterministic bounded workspace file matching for Foundation 1C."""

from __future__ import annotations

from pathlib import Path

from leonervis_code.core.contracts import ToolResult, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.tools import file_selector
from leonervis_code.tools.file_selector import (
    FileSelectionFailure,
    SelectorFailureKind,
    SelectorLimits,
    select_files,
)

GLOB_TOOL_NAME = "glob"
MAX_GLOB_PATTERN_CHARACTERS = file_selector.MAX_PATTERN_CHARACTERS
MAX_GLOB_PATTERN_BYTES = file_selector.MAX_PATTERN_BYTES
MAX_GLOB_PATTERN_COMPONENTS = file_selector.MAX_PATTERN_COMPONENTS
MAX_GLOB_MATCHES = 200
MAX_GLOB_OUTPUT_BYTES = 32 * 1024
MAX_GLOB_SCANNED_ENTRIES = file_selector.MAX_SCANNED_ENTRIES
MAX_GLOB_SCANNED_DIRECTORIES = file_selector.MAX_SCANNED_DIRECTORIES
MAX_GLOB_DEPTH = file_selector.MAX_DEPTH
GLOB_TRUNCATION_MARKER = "[truncated]\n"


def glob_model_definition() -> dict[str, object]:
    """Return a fresh provider-neutral definition of the bounded glob tool."""
    return {
        "name": GLOB_TOOL_NAME,
        "description": (
            "Match regular files using one portable workspace-relative '/'-separated glob "
            "pattern when file names or locations are needed before choosing files to read. "
            "This tool is read-only, bounded, deterministic, and never returns or traverses "
            "symbolic links. It supports component *, ?, bracket classes, and a whole-component "
            "**. Leading-dot names require an explicit leading dot in the matching component."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Portable workspace-relative '/'-separated regular-file glob pattern."
                    ),
                }
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    }


def glob_tool_snapshot() -> CanonicalToolDefinition:
    """Freeze the canonical neutral glob tool for effective-context identity."""
    return CanonicalToolDefinition.from_mapping(glob_model_definition())


class GlobTool:
    """Match regular files without leaving, mutating, or linking through one workspace."""

    def __init__(self, workspace: Path) -> None:
        """Resolve and validate the directory that bounds every search."""
        self._workspace = workspace.resolve()
        if not self._workspace.is_dir():
            raise ValueError("workspace must be an existing directory")

    def execute(self, request: ToolUse) -> ToolResult:
        """Return bounded stable matches for the request's pattern argument."""
        try:
            arguments = request.arguments.as_mapping()
            if set(arguments) != {"pattern"} or not isinstance(arguments["pattern"], str):
                return self._error(request, "glob input is malformed")
            matches = [
                selected.relative_path
                for selected in select_files(
                    self._workspace,
                    arguments["pattern"],
                    limits=SelectorLimits(
                        max_pattern_characters=MAX_GLOB_PATTERN_CHARACTERS,
                        max_pattern_bytes=MAX_GLOB_PATTERN_BYTES,
                        max_pattern_components=MAX_GLOB_PATTERN_COMPONENTS,
                        max_scanned_entries=MAX_GLOB_SCANNED_ENTRIES,
                        max_scanned_directories=MAX_GLOB_SCANNED_DIRECTORIES,
                        max_depth=MAX_GLOB_DEPTH,
                    ),
                )
            ]
            content, truncated = _format_matches(matches)
        except (AttributeError, ValueError):
            return self._error(request, "glob input is malformed")
        except FileSelectionFailure as error:
            return self._error(request, _glob_failure_message(error.kind))
        return ToolResult(
            tool_use_id=request.tool_use_id,
            content=content,
            truncated=truncated,
        )

    @staticmethod
    def _error(request: ToolUse, content: str) -> ToolResult:
        return ToolResult(tool_use_id=request.tool_use_id, content=content, is_error=True)


def _glob_failure_message(kind: SelectorFailureKind) -> str:
    messages = {
        SelectorFailureKind.BLANK_PATTERN: "glob pattern must not be blank",
        SelectorFailureKind.INVALID_COMPONENT: (
            "glob pattern contains an unsupported path component"
        ),
        SelectorFailureKind.OVERSIZED_PATTERN: "glob pattern exceeds the supported size",
        SelectorFailureKind.NONPORTABLE_PATTERN: (
            "glob pattern must be workspace-relative and use '/' separators"
        ),
        SelectorFailureKind.DIRECTORY_LIMIT: (
            "glob directory limit reached; use a narrower pattern"
        ),
        SelectorFailureKind.UNREADABLE_DIRECTORY: ("glob encountered an unreadable directory"),
        SelectorFailureKind.SCAN_FAILED: "glob could not scan the workspace",
        SelectorFailureKind.INVALID_UTF8_PATH: ("glob encountered a path that is not valid UTF-8"),
        SelectorFailureKind.ENTRY_LIMIT: (
            "glob traversal entry limit reached; use a narrower pattern"
        ),
        SelectorFailureKind.DEPTH_LIMIT: "glob depth limit reached; use a narrower pattern",
        SelectorFailureKind.FILE_LIMIT: "glob file limit reached; use a narrower pattern",
    }
    return messages[kind]


def _format_matches(matches: list[str]) -> tuple[str, bool]:
    count_truncated = len(matches) > MAX_GLOB_MATCHES
    selected = matches[:MAX_GLOB_MATCHES]
    complete = "".join(f"{path}\n" for path in selected)
    if not count_truncated and len(complete.encode("utf-8")) <= MAX_GLOB_OUTPUT_BYTES:
        return complete, False

    marker_bytes = len(GLOB_TRUNCATION_MARKER.encode("utf-8"))
    output: list[str] = []
    output_bytes = 0
    for path in selected:
        line = f"{path}\n"
        line_bytes = len(line.encode("utf-8"))
        if output_bytes + line_bytes + marker_bytes > MAX_GLOB_OUTPUT_BYTES:
            break
        output.append(line)
        output_bytes += line_bytes
    output.append(GLOB_TRUNCATION_MARKER)
    return "".join(output), True
