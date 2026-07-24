"""Bounded failure-atomic UTF-8 workspace writes with exact state preconditions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os
from pathlib import Path, PureWindowsPath
import stat
from uuid import uuid4

from leonervis_code.core.actions import ActionPrecondition, ActionPreconditionKind
from leonervis_code.core.contracts import ToolResult, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.core.permissions import PermissionAction

WRITE_FILE_TOOL_NAME = "write_file"
MAX_WRITE_CONTENT_CHARACTERS = 4096
MAX_WRITE_CONTENT_BYTES = 4096
MAX_OVERWRITE_SOURCE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PreparedWriteFile:
    """One trusted classification of an exact write request."""

    request: ToolUse
    relative_path: str
    content: bytes
    action: PermissionAction
    precondition: ActionPrecondition


class WriteFilePreparationError(ValueError):
    """A safe hard-bound rejection before an action is permission-eligible."""


class WriteFileOutcome(StrEnum):
    """Known result classes, including effects that are visible but only partially durable."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class WriteFileExecutionResult:
    """One truthful model result plus stable Host audit attribution."""

    tool_result: ToolResult
    outcome: WriteFileOutcome
    result_code: str
    audit_message: str


class WriteFilePartialEffectError(RuntimeError):
    """Report a visible target effect whose cleanup or durability is incomplete."""

    def __init__(self, result_code: str, message: str) -> None:
        self.result_code = result_code
        super().__init__(message)


class WriteFileTool:
    """Create or replace one bounded workspace text file without following symlinks."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        if not self._workspace.is_dir():
            raise ValueError("workspace must be an existing directory")

    def prepare(self, request: ToolUse) -> PreparedWriteFile:
        """Validate arguments and bind create/overwrite to the observed target state."""
        try:
            arguments = request.arguments.as_mapping()
            if set(arguments) != {"content", "path"}:
                raise ValueError
            raw_path = arguments["path"]
            content = arguments["content"]
            if not isinstance(raw_path, str) or not isinstance(content, str):
                raise ValueError
        except (AttributeError, ValueError):
            raise WriteFilePreparationError("write_file input is malformed") from None

        relative_path, target = self._target(raw_path)
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WriteFilePreparationError("write_file content must be valid UTF-8") from None
        if len(content) > MAX_WRITE_CONTENT_CHARACTERS or len(encoded) > MAX_WRITE_CONTENT_BYTES:
            raise WriteFilePreparationError(
                f"write_file content exceeds {MAX_WRITE_CONTENT_BYTES} bytes"
            )

        observed = self._observe(target)
        if observed is None:
            action = PermissionAction.WORKSPACE_CREATE
            precondition = ActionPrecondition.path_absent()
        else:
            action = PermissionAction.WORKSPACE_OVERWRITE
            precondition = ActionPrecondition.expected_state(observed.digest)
        return PreparedWriteFile(request, relative_path, encoded, action, precondition)

    def refresh_precondition(self, prepared: PreparedWriteFile) -> ActionPrecondition:
        """Re-observe target state for stale approval and lost-update checks."""
        _, target = self._target(prepared.relative_path)
        observed = self._observe(target)
        if observed is None:
            return ActionPrecondition.path_absent()
        return ActionPrecondition.expected_state(observed.digest)

    def execute(self, prepared: PreparedWriteFile) -> ToolResult:
        """Apply one prepared write and return its model-visible result."""
        return self.execute_detailed(prepared).tool_result

    def execute_detailed(self, prepared: PreparedWriteFile) -> WriteFileExecutionResult:
        """Apply exactly the prepared write with truthful partial-effect attribution."""
        request = prepared.request
        try:
            _, target = self._target(prepared.relative_path)
            if prepared.precondition.kind == ActionPreconditionKind.PATH_ABSENT:
                self._create(target, prepared.content)
                operation = "created"
            elif prepared.precondition.kind == ActionPreconditionKind.EXPECTED_STATE_SHA256:
                assert prepared.precondition.fingerprint is not None
                self._overwrite(target, prepared.content, prepared.precondition.fingerprint)
                operation = "overwritten"
            else:
                raise WriteFilePreparationError("write_file precondition is invalid")
        except WriteFilePartialEffectError as error:
            return WriteFileExecutionResult(
                ToolResult(request.tool_use_id, str(error), is_error=True),
                WriteFileOutcome.PARTIAL,
                error.result_code,
                str(error),
            )
        except WriteFilePreparationError as error:
            return WriteFileExecutionResult(
                ToolResult(request.tool_use_id, str(error), is_error=True),
                WriteFileOutcome.FAILED,
                "write_not_applied",
                str(error),
            )
        result = ToolResult(
            request.tool_use_id,
            (
                f'{{"bytes_written":{len(prepared.content)},"operation":"{operation}",'
                f'"path":"{_json_string(prepared.relative_path)}"}}\n'
            ),
        )
        return WriteFileExecutionResult(
            result,
            WriteFileOutcome.SUCCEEDED,
            operation,
            f"write_file {operation} {prepared.relative_path}",
        )

    def _target(self, raw_path: str) -> tuple[str, Path]:
        if (
            not raw_path
            or not raw_path.strip()
            or "\x00" in raw_path
            or "\\" in raw_path
            or Path(raw_path).is_absolute()
            or PureWindowsPath(raw_path).drive
        ):
            raise WriteFilePreparationError(
                "write_file path must be a portable workspace-relative file path"
            )
        parts = tuple(raw_path.split("/"))
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise WriteFilePreparationError(
                "write_file path must be a portable workspace-relative file path"
            )
        relative_path = "/".join(parts)
        current = self._workspace
        for component in parts[:-1]:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                raise WriteFilePreparationError(
                    "write_file parent directory does not exist"
                ) from None
            except PermissionError:
                raise WriteFilePreparationError(
                    "write_file parent directory is not accessible"
                ) from None
            except OSError:
                raise WriteFilePreparationError(
                    "write_file could not inspect parent directory"
                ) from None
            if stat.S_ISLNK(info.st_mode):
                raise WriteFilePreparationError("write_file path contains a symbolic link")
            if not stat.S_ISDIR(info.st_mode):
                raise WriteFilePreparationError("write_file parent path is not a directory")
        return relative_path, current / parts[-1]

    @staticmethod
    def _observe(target: Path) -> _ObservedFile | None:
        try:
            info = target.lstat()
        except FileNotFoundError:
            return None
        except PermissionError:
            raise WriteFilePreparationError("write_file target is not accessible") from None
        except OSError:
            raise WriteFilePreparationError("write_file could not inspect target") from None
        if stat.S_ISLNK(info.st_mode):
            raise WriteFilePreparationError("write_file target must not be a symbolic link")
        if not stat.S_ISREG(info.st_mode):
            raise WriteFilePreparationError("write_file target must be a regular file")
        if info.st_size > MAX_OVERWRITE_SOURCE_BYTES:
            raise WriteFilePreparationError(
                f"write_file existing file exceeds {MAX_OVERWRITE_SOURCE_BYTES} bytes"
            )
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except PermissionError:
            raise WriteFilePreparationError("write_file target is not readable") from None
        except OSError:
            raise WriteFilePreparationError("write_file could not read target") from None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise WriteFilePreparationError("write_file target must be a regular file")
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise WriteFilePreparationError("write_file target changed during inspection")
            data = _read_bounded(descriptor, MAX_OVERWRITE_SOURCE_BYTES)
        finally:
            os.close(descriptor)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise WriteFilePreparationError(
                "write_file existing content is not valid UTF-8"
            ) from None
        return _ObservedFile(
            content=data,
            digest=hashlib.sha256(data).hexdigest(),
            device=opened.st_dev,
            inode=opened.st_ino,
            mode=stat.S_IMODE(opened.st_mode),
        )

    def _create(self, target: Path, content: bytes) -> None:
        if self._observe(target) is not None:
            raise WriteFilePreparationError("write_file conflict: target is no longer absent")
        temporary = target.with_name(f".{target.name}.leonervis-{uuid4().hex}.tmp")
        descriptor = None
        installed = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o666,
            )
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                raise WriteFilePreparationError(
                    "write_file conflict: target is no longer absent"
                ) from None
            except OSError:
                raise WriteFilePreparationError("write_file could not create target") from None
            installed = True
            try:
                os.unlink(temporary)
            except OSError:
                try:
                    _fsync_directory(target.parent)
                except OSError:
                    raise WriteFilePartialEffectError(
                        "created_cleanup_and_durability_unknown",
                        "write_file created the target, but temporary cleanup failed and directory durability is unknown; inspect the workspace and do not retry automatically",
                    ) from None
                raise WriteFilePartialEffectError(
                    "created_with_temporary_cleanup_failure",
                    "write_file created the target durably, but temporary cleanup failed; inspect the workspace and do not retry automatically",
                ) from None
            try:
                _fsync_directory(target.parent)
            except OSError:
                raise WriteFilePartialEffectError(
                    "created_durability_unknown",
                    "write_file created the target, but directory durability is unknown; inspect the workspace and do not retry automatically",
                ) from None
        except (WriteFilePreparationError, WriteFilePartialEffectError):
            raise
        except PermissionError:
            raise WriteFilePreparationError("write_file target is not writable") from None
        except OSError:
            raise WriteFilePreparationError("write_file could not create target") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not installed:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _overwrite(self, target: Path, content: bytes, expected_digest: str) -> None:
        observed = self._observe(target)
        if observed is None or observed.digest != expected_digest:
            raise WriteFilePreparationError(
                "write_file conflict: target no longer matches the approved state"
            )
        temporary = target.with_name(f".{target.name}.leonervis-{uuid4().hex}.tmp")
        descriptor = None
        installed = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                observed.mode,
            )
            os.fchmod(descriptor, observed.mode)
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None

            current = self._observe(target)
            if (
                current is None
                or current.digest != expected_digest
                or (current.device, current.inode) != (observed.device, observed.inode)
            ):
                raise WriteFilePreparationError(
                    "write_file conflict: target no longer matches the approved state"
                )
            os.replace(temporary, target)
            installed = True
            try:
                _fsync_directory(target.parent)
            except OSError:
                raise WriteFilePartialEffectError(
                    "overwritten_durability_unknown",
                    "write_file replaced the target, but directory durability is unknown; inspect the workspace and do not retry automatically",
                ) from None
        except (WriteFilePreparationError, WriteFilePartialEffectError):
            raise
        except PermissionError:
            raise WriteFilePreparationError("write_file target is not writable") from None
        except OSError:
            if installed:
                raise WriteFilePartialEffectError(
                    "overwritten_durability_unknown",
                    "write_file replaced the target, but directory durability is unknown; inspect the workspace and do not retry automatically",
                ) from None
            raise WriteFilePreparationError("write_file could not replace target") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not installed:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


@dataclass(frozen=True)
class _ObservedFile:
    content: bytes
    digest: str
    device: int
    inode: int
    mode: int


def write_file_model_definition() -> dict[str, object]:
    """Return the exact provider-neutral controlled write definition."""
    return {
        "name": WRITE_FILE_TOOL_NAME,
        "description": (
            "Write bounded UTF-8 text to one workspace-relative file. The Host detects whether "
            "the action creates or overwrites, applies permission and approval policy, rejects "
            "symlinks, and uses exact target-state conflict checks before atomic installation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Portable workspace-relative destination file path.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        f"Complete UTF-8 file content, at most {MAX_WRITE_CONTENT_BYTES} bytes."
                    ),
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    }


def write_file_tool_snapshot() -> CanonicalToolDefinition:
    return CanonicalToolDefinition.from_mapping(write_file_model_definition())


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise WriteFilePreparationError(
            f"write_file existing file exceeds {MAX_OVERWRITE_SOURCE_BYTES} bytes"
        )
    return data


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_string(value: str) -> str:
    # Paths were validated as UTF-8 text; escape only JSON syntax/control characters.
    import json

    return json.dumps(value, ensure_ascii=False)[1:-1]
