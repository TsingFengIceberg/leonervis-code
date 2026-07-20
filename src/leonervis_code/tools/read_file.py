"""Read-only workspace file access for Foundation 1B."""

from __future__ import annotations

from pathlib import Path

from leonervis_code.core.contracts import ToolResult, ToolUse

MAX_CONTENT_BYTES = 32 * 1024
TRUNCATION_MARKER = "\n[truncated]\n"
READ_FILE_TOOL_NAME = "read_file"
MAX_READ_FILE_EXECUTIONS_PER_TURN = 3


def read_file_model_definition() -> dict[str, object]:
    """Return a fresh provider-neutral definition of the bounded read tool."""
    return {
        "name": READ_FILE_TOOL_NAME,
        "description": (
            "Read one workspace-relative UTF-8 text file when its contents are needed to "
            "answer the user. This tool is read-only and its bounded output may be truncated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to one UTF-8 text file in the workspace.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }


class ReadFileTool:
    """Read UTF-8 files only when their resolved paths remain inside one workspace."""

    def __init__(self, workspace: Path) -> None:
        """Resolve and validate the directory that bounds every file read."""
        self._workspace = workspace.resolve()
        if not self._workspace.is_dir():
            raise ValueError("workspace must be an existing directory")

    def execute(self, request: ToolUse) -> ToolResult:
        """Return a bounded result for ``request.path`` without raising path errors."""
        path = Path(request.path)
        if path.is_absolute():
            return self._error(request, "read_file path must be relative to the workspace")

        try:
            target = (self._workspace / path).resolve()
        except (OSError, RuntimeError, ValueError):
            return self._error(request, "read_file could not resolve path")

        if not target.is_relative_to(self._workspace):
            return self._error(request, "read_file path escapes the workspace")
        if not target.exists():
            return self._error(request, "read_file path does not exist")
        if not target.is_file():
            return self._error(request, "read_file path is not a file")

        try:
            with target.open("rb") as file:
                content = file.read(MAX_CONTENT_BYTES + 1)
        except PermissionError:
            return self._error(request, "read_file path is not readable")
        except OSError:
            return self._error(request, "read_file could not read path")

        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return self._error(request, "read_file content is not valid UTF-8")

        if len(content) <= MAX_CONTENT_BYTES:
            return ToolResult(tool_use_id=request.tool_use_id, content=content.decode("utf-8"))

        marker_size = len(TRUNCATION_MARKER.encode("utf-8"))
        prefix = content[: MAX_CONTENT_BYTES - marker_size]
        try:
            decoded_prefix = prefix.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.reason != "unexpected end of data":
                return self._error(request, "read_file content is not valid UTF-8")
            try:
                decoded_prefix = prefix[: error.start].decode("utf-8")
            except UnicodeDecodeError:
                return self._error(request, "read_file content is not valid UTF-8")

        return ToolResult(
            tool_use_id=request.tool_use_id,
            content=f"{decoded_prefix}{TRUNCATION_MARKER}",
            truncated=True,
        )

    @staticmethod
    def _error(request: ToolUse, content: str) -> ToolResult:
        """Create one model-visible read failure for ``request``."""
        return ToolResult(tool_use_id=request.tool_use_id, content=content, is_error=True)
