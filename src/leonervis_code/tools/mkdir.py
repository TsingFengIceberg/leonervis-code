"""Controlled creation of one bounded workspace directory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat

from leonervis_code.core.actions import ActionPrecondition, ActionPreconditionKind
from leonervis_code.core.contracts import ToolResult, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.core.permissions import PermissionAction

MKDIR_TOOL_NAME = "mkdir"
MAX_MKDIR_PATH_CHARACTERS = 4096
MAX_MKDIR_PATH_BYTES = 4096
MAX_MKDIR_PATH_COMPONENTS = 64
MAX_MKDIR_COMPONENT_BYTES = 255


@dataclass(frozen=True)
class PreparedMkdir:
    """One exact absent directory target prepared without filesystem mutation."""

    request: ToolUse
    relative_path: str
    action: PermissionAction
    precondition: ActionPrecondition


class MkdirPreparationError(ValueError):
    """A hard-bound rejection before directory creation is permission-eligible."""


class MkdirOutcome(StrEnum):
    """Known directory creation outcomes including uncertain durability."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class MkdirExecutionResult:
    """One truthful model result plus stable Host audit attribution."""

    tool_result: ToolResult
    outcome: MkdirOutcome
    result_code: str
    audit_message: str


class MkdirPartialEffectError(RuntimeError):
    """Report a visible created directory whose durability is uncertain."""


class MkdirTool:
    """Create exactly one missing workspace directory without following symlinks."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        if not self._workspace.is_dir():
            raise ValueError("workspace must be an existing directory")

    def prepare(self, request: ToolUse) -> PreparedMkdir:
        """Validate one path and bind the request to its observed absence."""
        try:
            arguments = request.arguments.as_mapping()
            if set(arguments) != {"path"}:
                raise ValueError
            raw_path = arguments["path"]
            if not isinstance(raw_path, str):
                raise ValueError
        except (AttributeError, ValueError):
            raise MkdirPreparationError("mkdir input is malformed") from None

        relative_path, target = self._target(raw_path)
        if self._observe(target) is not None:
            raise MkdirPreparationError("mkdir target already exists")
        return PreparedMkdir(
            request=request,
            relative_path=relative_path,
            action=PermissionAction.WORKSPACE_CREATE,
            precondition=ActionPrecondition.path_absent(),
        )

    def refresh_precondition(self, prepared: PreparedMkdir) -> ActionPrecondition:
        """Re-observe target absence for stale approval and race checks."""
        if type(prepared) is not PreparedMkdir:
            raise ValueError("prepared mkdir is invalid")
        _, target = self._target(prepared.relative_path)
        observed = self._observe(target)
        if observed is None:
            return ActionPrecondition.path_absent()
        payload = (
            f"{observed.st_dev}:{observed.st_ino}:{observed.st_mode}:"
            f"{observed.st_size}:{observed.st_mtime_ns}"
        ).encode("ascii")
        return ActionPrecondition.expected_state(hashlib.sha256(payload).hexdigest())

    def execute(self, prepared: PreparedMkdir) -> ToolResult:
        """Create one prepared directory and return its model-visible result."""
        return self.execute_detailed(prepared).tool_result

    def execute_detailed(self, prepared: PreparedMkdir) -> MkdirExecutionResult:
        """Create exactly one directory with truthful durability attribution."""
        if type(prepared) is not PreparedMkdir:
            raise ValueError("prepared mkdir is invalid")
        request = prepared.request
        created = False
        try:
            if prepared.precondition.kind != ActionPreconditionKind.PATH_ABSENT:
                raise MkdirPreparationError("mkdir precondition is invalid")
            _, target = self._target(prepared.relative_path)
            if self._observe(target) is not None:
                raise MkdirPreparationError("mkdir conflict: target is no longer absent")
            try:
                target.mkdir()
            except FileExistsError:
                raise MkdirPreparationError("mkdir conflict: target is no longer absent") from None
            created = True
            try:
                _fsync_directory(target)
                _fsync_directory(target.parent)
            except OSError:
                raise MkdirPartialEffectError(
                    "mkdir created the directory, but durability is unknown; inspect the workspace and do not retry automatically"
                ) from None
        except MkdirPartialEffectError as error:
            return MkdirExecutionResult(
                ToolResult(request.tool_use_id, str(error), is_error=True),
                MkdirOutcome.PARTIAL,
                "directory_created_durability_unknown",
                str(error),
            )
        except MkdirPreparationError as error:
            return MkdirExecutionResult(
                ToolResult(request.tool_use_id, str(error), is_error=True),
                MkdirOutcome.FAILED,
                "directory_not_created",
                str(error),
            )
        except PermissionError:
            if created:
                message = (
                    "mkdir created the directory, but durability is unknown; inspect the "
                    "workspace and do not retry automatically"
                )
                return MkdirExecutionResult(
                    ToolResult(request.tool_use_id, message, is_error=True),
                    MkdirOutcome.PARTIAL,
                    "directory_created_durability_unknown",
                    message,
                )
            message = "mkdir target is not writable"
            return MkdirExecutionResult(
                ToolResult(request.tool_use_id, message, is_error=True),
                MkdirOutcome.FAILED,
                "directory_not_created",
                message,
            )
        except OSError:
            if created:
                message = (
                    "mkdir created the directory, but durability is unknown; inspect the "
                    "workspace and do not retry automatically"
                )
                return MkdirExecutionResult(
                    ToolResult(request.tool_use_id, message, is_error=True),
                    MkdirOutcome.PARTIAL,
                    "directory_created_durability_unknown",
                    message,
                )
            message = "mkdir could not create target"
            return MkdirExecutionResult(
                ToolResult(request.tool_use_id, message, is_error=True),
                MkdirOutcome.FAILED,
                "directory_not_created",
                message,
            )

        payload = json.dumps(
            {"operation": "created", "path": prepared.relative_path},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return MkdirExecutionResult(
            ToolResult(request.tool_use_id, f"{payload}\n"),
            MkdirOutcome.SUCCEEDED,
            "directory_created",
            f"mkdir created {prepared.relative_path}",
        )

    def _target(self, raw_path: str) -> tuple[str, Path]:
        try:
            encoded_path = raw_path.encode("utf-8")
        except UnicodeEncodeError:
            raise MkdirPreparationError("mkdir path must be valid UTF-8") from None
        if (
            not raw_path
            or not raw_path.strip()
            or len(raw_path) > MAX_MKDIR_PATH_CHARACTERS
            or len(encoded_path) > MAX_MKDIR_PATH_BYTES
            or "\x00" in raw_path
            or "\\" in raw_path
            or Path(raw_path).is_absolute()
            or PureWindowsPath(raw_path).drive
        ):
            raise MkdirPreparationError(
                "mkdir path must be a portable workspace-relative directory path"
            )
        parts = tuple(raw_path.split("/"))
        if (
            not parts
            or len(parts) > MAX_MKDIR_PATH_COMPONENTS
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise MkdirPreparationError(
                "mkdir path must be a portable workspace-relative directory path"
            )
        for part in parts:
            try:
                encoded_part = part.encode("utf-8")
            except UnicodeEncodeError:
                raise MkdirPreparationError("mkdir path must be valid UTF-8") from None
            if len(encoded_part) > MAX_MKDIR_COMPONENT_BYTES:
                raise MkdirPreparationError(
                    f"mkdir path component exceeds {MAX_MKDIR_COMPONENT_BYTES} bytes"
                )

        relative_path = "/".join(parts)
        current = self._workspace
        for component in parts[:-1]:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                raise MkdirPreparationError("mkdir parent directory does not exist") from None
            except PermissionError:
                raise MkdirPreparationError("mkdir parent directory is not accessible") from None
            except OSError:
                raise MkdirPreparationError("mkdir could not inspect parent directory") from None
            if stat.S_ISLNK(info.st_mode):
                raise MkdirPreparationError("mkdir path contains a symbolic link")
            if not stat.S_ISDIR(info.st_mode):
                raise MkdirPreparationError("mkdir parent path is not a directory")
        return relative_path, current / parts[-1]

    @staticmethod
    def _observe(target: Path) -> os.stat_result | None:
        try:
            return target.lstat()
        except FileNotFoundError:
            return None
        except PermissionError:
            raise MkdirPreparationError("mkdir target is not accessible") from None
        except OSError:
            raise MkdirPreparationError("mkdir could not inspect target") from None


def mkdir_model_definition() -> dict[str, object]:
    """Return the exact provider-neutral controlled directory definition."""
    return {
        "name": MKDIR_TOOL_NAME,
        "description": (
            "Create exactly one missing workspace-relative directory. The parent must already "
            "exist. The Host applies workspace-create permission and approval policy, rejects "
            "symlinks and stale targets, and does not create parent directories recursively."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Portable workspace-relative path of the directory to create.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }


def mkdir_tool_snapshot() -> CanonicalToolDefinition:
    return CanonicalToolDefinition.from_mapping(mkdir_model_definition())


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
