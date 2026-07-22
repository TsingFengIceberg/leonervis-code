"""Fixed ordered model-tool contract for the current read-only surface."""

from __future__ import annotations

from leonervis_code.core.contracts import ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.tools.glob import GLOB_TOOL_NAME, glob_tool_snapshot
from leonervis_code.tools.read_file import READ_FILE_TOOL_NAME, read_file_tool_snapshot

MAX_TOOL_EXECUTIONS_PER_TURN = 3

TOOL_CATALOG: tuple[CanonicalToolDefinition, ...] = (
    read_file_tool_snapshot(),
    glob_tool_snapshot(),
)


def model_tool_definitions() -> tuple[dict[str, object], ...]:
    """Return fresh definitions in the canonical model-visible order."""
    return tuple(definition.as_mapping() for definition in TOOL_CATALOG)


def tool_operand_key(name: str) -> str:
    """Return the closed provider-visible key for one known tool operand."""
    if name == READ_FILE_TOOL_NAME:
        return "path"
    if name == GLOB_TOOL_NAME:
        return "pattern"
    raise ValueError(f"unsupported tool: {name}")


def tool_input_from_use(request: ToolUse) -> dict[str, str]:
    """Project the neutral single-string operand for one known tool."""
    return {tool_operand_key(request.name): request.path}
