"""Fixed ordered model-tool contract for the current bounded workspace surface."""

from __future__ import annotations

from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.tools.edit_file import EDIT_FILE_TOOL_NAME, edit_file_tool_snapshot
from leonervis_code.tools.glob import GLOB_TOOL_NAME, glob_tool_snapshot
from leonervis_code.tools.grep import GREP_TOOL_NAME, grep_tool_snapshot
from leonervis_code.tools.mkdir import MKDIR_TOOL_NAME, mkdir_tool_snapshot
from leonervis_code.tools.read_file import READ_FILE_TOOL_NAME, read_file_tool_snapshot
from leonervis_code.tools.run_command import (
    MAX_COMMAND_ARGUMENTS,
    MAX_COMMAND_ARGUMENT_BYTES,
    MAX_COMMAND_ARGUMENT_CHARACTERS,
    MAX_COMMAND_ARGV_BYTES,
    MAX_COMMAND_CWD_BYTES,
    MAX_COMMAND_CWD_CHARACTERS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MIN_COMMAND_TIMEOUT_SECONDS,
    RUN_COMMAND_TOOL_NAME,
    run_command_tool_snapshot,
)
from leonervis_code.tools.write_file import WRITE_FILE_TOOL_NAME, write_file_tool_snapshot

MAX_TOOL_EXECUTIONS_PER_TURN = 3
MAX_TOOL_INPUT_STRING_CHARACTERS = 4096
MAX_TOOL_INPUT_STRING_BYTES = 4096

TOOL_CATALOG: tuple[CanonicalToolDefinition, ...] = (
    read_file_tool_snapshot(),
    glob_tool_snapshot(),
    grep_tool_snapshot(),
    write_file_tool_snapshot(),
    edit_file_tool_snapshot(),
    run_command_tool_snapshot(),
    mkdir_tool_snapshot(),
)


def model_tool_definitions() -> tuple[dict[str, object], ...]:
    """Return fresh definitions in the canonical model-visible order."""
    return tuple(definition.as_mapping() for definition in TOOL_CATALOG)


def tool_use_from_input(
    tool_use_id: str,
    name: str,
    tool_input: dict[str, object],
) -> ToolUse:
    """Validate one exact known-tool input and freeze its neutral arguments."""
    expected = _expected_keys(name)
    if not isinstance(tool_input, dict) or set(tool_input) != expected:
        raise ValueError(f"{name} input is malformed")
    _validate_known_input(name, tool_input, expected)
    return ToolUse(
        tool_use_id=tool_use_id,
        name=name,
        arguments=ToolArguments.from_mapping(tool_input),
    )


def tool_input_from_use(request: ToolUse) -> dict[str, object]:
    """Project and revalidate immutable arguments for one known tool."""
    if not isinstance(request.arguments, ToolArguments):
        raise ValueError("tool arguments are invalid")
    tool_input = request.arguments.as_mapping()
    expected = _expected_keys(request.name)
    if set(tool_input) != expected:
        raise ValueError(f"{request.name} input is malformed")
    _validate_known_input(request.name, tool_input, expected)
    return tool_input


def _expected_keys(name: str) -> set[str]:
    if name == READ_FILE_TOOL_NAME:
        return {"path"}
    if name == GLOB_TOOL_NAME:
        return {"pattern"}
    if name == GREP_TOOL_NAME:
        return {"query", "include"}
    if name == WRITE_FILE_TOOL_NAME:
        return {"path", "content"}
    if name == EDIT_FILE_TOOL_NAME:
        return {"path", "old_text", "new_text"}
    if name == RUN_COMMAND_TOOL_NAME:
        return {"argv", "cwd", "timeout_seconds"}
    if name == MKDIR_TOOL_NAME:
        return {"path"}
    raise ValueError(f"unsupported tool: {name}")


def _validate_known_input(name: str, tool_input: dict[str, object], expected: set[str]) -> None:
    if name == RUN_COMMAND_TOOL_NAME:
        argv = tool_input["argv"]
        cwd = tool_input["cwd"]
        timeout = tool_input["timeout_seconds"]
        if not isinstance(argv, list) or not (1 <= len(argv) <= MAX_COMMAND_ARGUMENTS):
            raise ValueError(
                f"run_command argv must contain 1 to {MAX_COMMAND_ARGUMENTS} arguments"
            )
        total_bytes = 0
        for index, argument in enumerate(argv):
            _validate_input_string(
                argument,
                label=f"run_command argv[{index}]",
                allow_whitespace=index != 0,
                allow_empty=index != 0,
                max_characters=MAX_COMMAND_ARGUMENT_CHARACTERS,
                max_bytes=MAX_COMMAND_ARGUMENT_BYTES,
            )
            total_bytes += len(argument.encode("utf-8"))
        if total_bytes > MAX_COMMAND_ARGV_BYTES:
            raise ValueError(f"run_command argv exceeds {MAX_COMMAND_ARGV_BYTES} total bytes")
        _validate_input_string(
            cwd,
            label="run_command cwd",
            max_characters=MAX_COMMAND_CWD_CHARACTERS,
            max_bytes=MAX_COMMAND_CWD_BYTES,
        )
        if type(timeout) is not int or not (
            MIN_COMMAND_TIMEOUT_SECONDS <= timeout <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "run_command timeout_seconds must be an integer from "
                f"{MIN_COMMAND_TIMEOUT_SECONDS} to {MAX_COMMAND_TIMEOUT_SECONDS}"
            )
        return

    for key in expected:
        _validate_input_string(
            tool_input[key],
            label=f"{name} {key}",
            allow_whitespace=key in {"query", "content", "old_text", "new_text"},
            allow_empty=key in {"content", "new_text"},
        )


def _validate_input_string(
    value: object,
    *,
    label: str,
    allow_whitespace: bool = False,
    allow_empty: bool = False,
    max_characters: int = MAX_TOOL_INPUT_STRING_CHARACTERS,
    max_bytes: int = MAX_TOOL_INPUT_STRING_BYTES,
) -> None:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or (not allow_empty and not allow_whitespace and not value.strip())
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be nonblank text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} must be valid UTF-8") from None
    if len(value) > max_characters or len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the supported size")
