"""Prepared exact single-replacement edits over bounded workspace UTF-8 files."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path

from leonervis_code.core.actions import ActionPrecondition, ActionPreconditionKind
from leonervis_code.core.contracts import ToolResult, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.core.permissions import PermissionAction
from leonervis_code.tools.write_file import (
    MAX_OVERWRITE_SOURCE_BYTES,
    MAX_WRITE_CONTENT_BYTES,
    MAX_WRITE_CONTENT_CHARACTERS,
    WriteFilePartialEffectError,
    WriteFilePreparationError,
    WriteFileTool,
)

EDIT_FILE_TOOL_NAME = "edit_file"
MAX_EDIT_TEXT_CHARACTERS = MAX_WRITE_CONTENT_CHARACTERS
MAX_EDIT_TEXT_BYTES = MAX_WRITE_CONTENT_BYTES
MAX_EDIT_SOURCE_BYTES = MAX_OVERWRITE_SOURCE_BYTES
MAX_EDIT_RESULT_BYTES = MAX_OVERWRITE_SOURCE_BYTES


@dataclass(frozen=True)
class PreparedEditFile:
    """One side-effect-free exact edit bound to the observed source state."""

    request: ToolUse
    relative_path: str
    content: bytes
    action: PermissionAction
    precondition: ActionPrecondition


class EditFilePreparationError(ValueError):
    """A safe hard-bound rejection before an edit is permission-eligible."""


class EditFileOutcome(StrEnum):
    """Known exact-edit result classes, including visible partial durability."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class EditFileExecutionResult:
    """One truthful edit result plus stable Host audit attribution."""

    tool_result: ToolResult
    outcome: EditFileOutcome
    result_code: str
    audit_message: str


class EditFileTool:
    """Replace one unique exact string in one existing bounded workspace file."""

    def __init__(self, workspace: Path) -> None:
        self._write_boundary = WriteFileTool(workspace)

    def prepare(self, request: ToolUse) -> PreparedEditFile:
        """Validate and build an immutable candidate without changing the workspace."""
        try:
            arguments = request.arguments.as_mapping()
            if set(arguments) != {"new_text", "old_text", "path"}:
                raise ValueError
            raw_path = arguments["path"]
            old_text = arguments["old_text"]
            new_text = arguments["new_text"]
            if not all(isinstance(value, str) for value in (raw_path, old_text, new_text)):
                raise ValueError
        except (AttributeError, ValueError):
            raise EditFilePreparationError("edit_file input is malformed") from None

        assert isinstance(raw_path, str)
        assert isinstance(old_text, str)
        assert isinstance(new_text, str)
        if not old_text:
            raise EditFilePreparationError("edit_file old_text must not be empty")
        if old_text == new_text:
            raise EditFilePreparationError("edit_file replacement must change the file")
        self._validate_edit_text("old_text", old_text)
        self._validate_edit_text("new_text", new_text)

        try:
            relative_path, target = self._write_boundary._target(raw_path)
            observed = self._write_boundary._observe(target)
        except WriteFilePreparationError as error:
            raise EditFilePreparationError(_edit_message(error)) from None
        if observed is None:
            raise EditFilePreparationError("edit_file target must already exist")

        source = observed.content.decode("utf-8")
        match_start = source.find(old_text)
        if match_start < 0:
            raise EditFilePreparationError("edit_file old_text was not found")
        if source.find(old_text, match_start + 1) >= 0:
            raise EditFilePreparationError("edit_file old_text matches more than once")

        candidate = source[:match_start] + new_text + source[match_start + len(old_text) :]
        if candidate == source:
            raise EditFilePreparationError("edit_file replacement must change the file")
        try:
            encoded = candidate.encode("utf-8")
        except UnicodeEncodeError:
            raise EditFilePreparationError("edit_file result must be valid UTF-8") from None
        if len(encoded) > MAX_EDIT_RESULT_BYTES:
            raise EditFilePreparationError(
                f"edit_file result exceeds {MAX_EDIT_RESULT_BYTES} bytes"
            )

        return PreparedEditFile(
            request=request,
            relative_path=relative_path,
            content=encoded,
            action=PermissionAction.WORKSPACE_OVERWRITE,
            precondition=ActionPrecondition.expected_state(observed.digest),
        )

    @staticmethod
    def _validate_edit_text(argument: str, value: str) -> None:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise EditFilePreparationError(f"edit_file {argument} must be valid UTF-8") from None
        if len(value) > MAX_EDIT_TEXT_CHARACTERS or len(encoded) > MAX_EDIT_TEXT_BYTES:
            raise EditFilePreparationError(
                f"edit_file {argument} exceeds {MAX_EDIT_TEXT_BYTES} bytes"
            )

    def refresh_precondition(self, prepared: PreparedEditFile) -> ActionPrecondition:
        """Re-observe the source for stale approval and lost-update checks."""
        try:
            _, target = self._write_boundary._target(prepared.relative_path)
            observed = self._write_boundary._observe(target)
        except WriteFilePreparationError as error:
            raise EditFilePreparationError(_edit_message(error)) from None
        if observed is None:
            return ActionPrecondition.path_absent()
        return ActionPrecondition.expected_state(observed.digest)

    def execute(self, prepared: PreparedEditFile) -> ToolResult:
        """Apply one prepared exact edit and return its tool result."""
        return self.execute_detailed(prepared).tool_result

    def execute_detailed(self, prepared: PreparedEditFile) -> EditFileExecutionResult:
        """Atomically install the candidate with truthful failure attribution."""
        request = prepared.request
        try:
            if prepared.precondition.kind != ActionPreconditionKind.EXPECTED_STATE_SHA256:
                raise EditFilePreparationError("edit_file precondition is invalid")
            assert prepared.precondition.fingerprint is not None
            _, target = self._write_boundary._target(prepared.relative_path)
            self._write_boundary._overwrite(
                target,
                prepared.content,
                prepared.precondition.fingerprint,
            )
        except WriteFilePartialEffectError as error:
            message = _edit_message(error)
            result_code = (
                "edited_durability_unknown"
                if error.result_code == "overwritten_durability_unknown"
                else error.result_code
            )
            return EditFileExecutionResult(
                ToolResult(request.tool_use_id, message, is_error=True),
                EditFileOutcome.PARTIAL,
                result_code,
                message,
            )
        except (WriteFilePreparationError, EditFilePreparationError) as error:
            message = _edit_message(error)
            return EditFileExecutionResult(
                ToolResult(request.tool_use_id, message, is_error=True),
                EditFileOutcome.FAILED,
                "edit_not_applied",
                message,
            )

        payload = json.dumps(
            {
                "bytes_written": len(prepared.content),
                "operation": "edited",
                "path": prepared.relative_path,
                "replacements": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return EditFileExecutionResult(
            ToolResult(request.tool_use_id, f"{payload}\n"),
            EditFileOutcome.SUCCEEDED,
            "edited",
            f"edit_file edited {prepared.relative_path}",
        )


def _edit_message(error: BaseException) -> str:
    return str(error).replace("write_file", "edit_file", 1)


def edit_file_model_definition() -> dict[str, object]:
    """Return the exact provider-neutral controlled edit definition."""
    return {
        "name": EDIT_FILE_TOOL_NAME,
        "description": (
            "Replace one uniquely matching exact text fragment in one existing bounded UTF-8 "
            "workspace file. The Host applies overwrite permission and approval policy, rejects "
            "zero or multiple matches and symlinks, and rechecks the exact source state before "
            "atomic replacement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Portable workspace-relative path of an existing text file.",
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "Non-empty exact UTF-8 text that must occur exactly once, at most "
                        f"{MAX_EDIT_TEXT_BYTES} bytes."
                    ),
                },
                "new_text": {
                    "type": "string",
                    "description": (
                        "Exact replacement UTF-8 text, which may be empty, at most "
                        f"{MAX_EDIT_TEXT_BYTES} bytes."
                    ),
                },
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    }


def edit_file_tool_snapshot() -> CanonicalToolDefinition:
    return CanonicalToolDefinition.from_mapping(edit_file_model_definition())
