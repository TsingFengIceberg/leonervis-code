"""Bounded argv-based command preparation and execution for one workspace."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path, PureWindowsPath
import signal
import stat
import subprocess
from threading import Thread
import time

from leonervis_code.core.actions import ActionPrecondition
from leonervis_code.core.contracts import ToolResult, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition
from leonervis_code.core.permissions import PermissionAction

RUN_COMMAND_TOOL_NAME = "run_command"
MAX_COMMAND_ARGUMENTS = 64
MAX_COMMAND_ARGUMENT_CHARACTERS = 1024
MAX_COMMAND_ARGUMENT_BYTES = 1024
MAX_COMMAND_ARGV_BYTES = 8 * 1024
MAX_COMMAND_CWD_CHARACTERS = 4096
MAX_COMMAND_CWD_BYTES = 4096
MAX_COMMAND_CWD_COMPONENTS = 64
MIN_COMMAND_TIMEOUT_SECONDS = 1
MAX_COMMAND_TIMEOUT_SECONDS = 300
MAX_COMMAND_STDOUT_BYTES = 32 * 1024
MAX_COMMAND_STDERR_BYTES = 32 * 1024
COMMAND_TERMINATE_GRACE_SECONDS = 1.0
COMMAND_KILL_GRACE_SECONDS = 1.0
COMMAND_PIPE_DRAIN_GRACE_SECONDS = 1.0
COMMAND_ENVIRONMENT_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_COLOR",
    "PATH",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "UV_CACHE_DIR",
    "VIRTUAL_ENV",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)


@dataclass(frozen=True)
class PreparedRunCommand:
    """One exact command request prepared without starting a process."""

    request: ToolUse
    argv: tuple[str, ...]
    relative_cwd: str
    timeout_seconds: int
    action: PermissionAction
    precondition: ActionPrecondition


class RunCommandOutcome(StrEnum):
    """Known Host outcome after a command process may have started."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class RunCommandExecutionResult:
    tool_result: ToolResult
    outcome: RunCommandOutcome
    result_code: str
    audit_message: str


class RunCommandPreparationError(ValueError):
    """Reject malformed or unsafe-to-prepare command requests before permission policy."""


@dataclass
class _BoundedCapture:
    limit: int
    captured: bytearray
    total: int = 0
    error: bool = False

    def consume(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = self.limit - len(self.captured)
        if remaining > 0:
            self.captured.extend(chunk[:remaining])


class RunCommandTool:
    """Prepare and execute one direct bounded command without shell interpretation."""

    def __init__(self, workspace: Path, environment: Mapping[str, str] | None = None) -> None:
        self._workspace = workspace.resolve()
        if not self._workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        self._environment = dict(os.environ if environment is None else environment)

    def prepare(self, request: ToolUse) -> PreparedRunCommand:
        """Validate and freeze one exact command request without starting a process."""
        try:
            if request.name != RUN_COMMAND_TOOL_NAME:
                raise ValueError
            arguments = request.arguments.as_mapping()
            if set(arguments) != {"argv", "cwd", "timeout_seconds"}:
                raise ValueError
            raw_argv = arguments["argv"]
            raw_cwd = arguments["cwd"]
            timeout_seconds = arguments["timeout_seconds"]
            if not isinstance(raw_argv, list) or not isinstance(raw_cwd, str):
                raise ValueError
        except (AttributeError, ValueError):
            raise RunCommandPreparationError("run_command input is malformed") from None

        argv = self._validate_argv(raw_argv)
        relative_cwd = self._validate_cwd(raw_cwd)
        if type(timeout_seconds) is not int or not (
            MIN_COMMAND_TIMEOUT_SECONDS <= timeout_seconds <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise RunCommandPreparationError(
                "run_command timeout_seconds must be an integer from "
                f"{MIN_COMMAND_TIMEOUT_SECONDS} to {MAX_COMMAND_TIMEOUT_SECONDS}"
            )

        return PreparedRunCommand(
            request=request,
            argv=argv,
            relative_cwd=relative_cwd,
            timeout_seconds=timeout_seconds,
            action=PermissionAction.DANGEROUS,
            precondition=ActionPrecondition.none(),
        )

    def revalidate(self, prepared: PreparedRunCommand) -> ActionPrecondition:
        """Recheck the workspace root and cwd immediately before execution starts."""
        if type(prepared) is not PreparedRunCommand:
            raise ValueError("prepared run_command is invalid")
        self._validate_cwd(prepared.relative_cwd)
        return ActionPrecondition.none()

    def execute(self, prepared: PreparedRunCommand) -> ToolResult:
        """Execute one prepared command and return its structured model result."""
        return self.execute_detailed(prepared).tool_result

    def execute_detailed(self, prepared: PreparedRunCommand) -> RunCommandExecutionResult:
        """Run argv directly with bounded output, timeout, and process-group cleanup."""
        if type(prepared) is not PreparedRunCommand:
            raise ValueError("prepared run_command is invalid")
        request = prepared.request
        try:
            self._validate_cwd(prepared.relative_cwd)
        except RunCommandPreparationError:
            empty_stdout = _BoundedCapture(MAX_COMMAND_STDOUT_BYTES, bytearray())
            empty_stderr = _BoundedCapture(MAX_COMMAND_STDERR_BYTES, bytearray())
            return RunCommandExecutionResult(
                ToolResult(
                    request.tool_use_id,
                    self._payload(
                        prepared,
                        status="spawn-rejected",
                        returncode=None,
                        stdout=empty_stdout,
                        stderr=empty_stderr,
                        cleanup_complete=True,
                    ),
                    is_error=True,
                ),
                RunCommandOutcome.FAILED,
                "command_cwd_invalid",
                "run_command cwd no longer satisfies the prepared boundary",
            )
        cwd = (
            self._workspace
            if prepared.relative_cwd == "."
            else self._workspace / prepared.relative_cwd
        )
        environment = {
            key: value
            for key in COMMAND_ENVIRONMENT_ALLOWLIST
            if isinstance((value := self._environment.get(key)), str)
        }
        environment["PWD"] = str(cwd)
        stdout_capture = _BoundedCapture(MAX_COMMAND_STDOUT_BYTES, bytearray())
        stderr_capture = _BoundedCapture(MAX_COMMAND_STDERR_BYTES, bytearray())

        try:
            process = subprocess.Popen(
                prepared.argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError):
            payload = self._payload(
                prepared,
                status="spawn-failed",
                returncode=None,
                stdout=stdout_capture,
                stderr=stderr_capture,
                cleanup_complete=True,
            )
            return RunCommandExecutionResult(
                ToolResult(request.tool_use_id, payload, is_error=True),
                RunCommandOutcome.FAILED,
                "command_spawn_failed",
                "run_command could not start the requested executable",
            )

        assert process.stdout is not None and process.stderr is not None
        readers = (
            Thread(target=_drain_pipe, args=(process.stdout, stdout_capture), daemon=True),
            Thread(target=_drain_pipe, args=(process.stderr, stderr_capture), daemon=True),
        )
        for reader in readers:
            reader.start()

        status = "exited"
        cleanup_complete = True
        interrupted = False
        try:
            process.wait(timeout=prepared.timeout_seconds)
        except subprocess.TimeoutExpired:
            status = "timed-out"
            cleanup_complete = self._terminate_process_group(process)
        except KeyboardInterrupt:
            status = "cancelled"
            interrupted = True
            cleanup_complete = self._terminate_process_group(process)

        readers_complete = _join_readers(readers, COMMAND_PIPE_DRAIN_GRACE_SECONDS)
        if not readers_complete:
            cleanup_complete = self._terminate_process_group(process) and cleanup_complete
            for pipe in (process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            readers_complete = _join_readers(readers, COMMAND_PIPE_DRAIN_GRACE_SECONDS)
        cleanup_complete = (
            cleanup_complete
            and readers_complete
            and not (stdout_capture.error or stderr_capture.error)
        )

        returncode = process.poll()
        if returncode is None:
            cleanup_complete = False
        if interrupted:
            result_code = (
                "command_cancelled" if cleanup_complete else "command_cancel_cleanup_incomplete"
            )
            outcome = RunCommandOutcome.PARTIAL
        elif status == "timed-out":
            result_code = (
                "command_timed_out" if cleanup_complete else "command_timeout_cleanup_incomplete"
            )
            outcome = RunCommandOutcome.PARTIAL
        elif not cleanup_complete:
            status = "cleanup-incomplete"
            result_code = "command_cleanup_incomplete"
            outcome = RunCommandOutcome.PARTIAL
        elif returncode < 0:
            status = "signaled"
            result_code = "command_signaled"
            outcome = RunCommandOutcome.PARTIAL
        elif returncode == 0 and cleanup_complete:
            result_code = "command_succeeded"
            outcome = RunCommandOutcome.SUCCEEDED
        elif returncode == 0:
            status = "cleanup-incomplete"
            result_code = "command_cleanup_incomplete"
            outcome = RunCommandOutcome.PARTIAL
        else:
            result_code = "command_exited_nonzero"
            outcome = RunCommandOutcome.FAILED

        payload = self._payload(
            prepared,
            status=status,
            returncode=returncode,
            stdout=stdout_capture,
            stderr=stderr_capture,
            cleanup_complete=cleanup_complete,
        )
        is_error = outcome != RunCommandOutcome.SUCCEEDED
        audit_message = {
            "command_succeeded": "run_command exited successfully",
            "command_cwd_invalid": "run_command cwd no longer satisfies the prepared boundary",
            "command_exited_nonzero": "run_command exited with a nonzero status",
            "command_signaled": "run_command ended because of a signal",
            "command_timed_out": "run_command timed out and its process group was terminated",
            "command_timeout_cleanup_incomplete": "run_command timed out and cleanup is incomplete",
            "command_cancelled": "run_command was cancelled and its process group was terminated",
            "command_cancel_cleanup_incomplete": "run_command was cancelled and cleanup is incomplete",
            "command_cleanup_incomplete": "run_command process cleanup is incomplete",
        }[result_code]
        return RunCommandExecutionResult(
            ToolResult(
                request.tool_use_id,
                payload,
                is_error=is_error,
                truncated=(
                    stdout_capture.total > stdout_capture.limit
                    or stderr_capture.total > stderr_capture.limit
                ),
            ),
            outcome,
            result_code,
            audit_message,
        )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
        pgid = process.pid
        if not _signal_process_group(process, pgid, signal.SIGTERM):
            return False
        if _wait_for_process_group_exit(process, pgid, COMMAND_TERMINATE_GRACE_SECONDS):
            return True
        if not _signal_process_group(process, pgid, signal.SIGKILL):
            return False
        return _wait_for_process_group_exit(process, pgid, COMMAND_KILL_GRACE_SECONDS)

    @staticmethod
    def _payload(
        prepared: PreparedRunCommand,
        *,
        status: str,
        returncode: int | None,
        stdout: _BoundedCapture,
        stderr: _BoundedCapture,
        cleanup_complete: bool,
    ) -> str:
        exit_code = returncode if returncode is not None and returncode >= 0 else None
        signal_number = -returncode if returncode is not None and returncode < 0 else None
        return (
            json.dumps(
                {
                    "cleanup_complete": cleanup_complete,
                    "cwd": prepared.relative_cwd,
                    "exit_code": exit_code,
                    "signal": signal_number,
                    "status": status,
                    "stderr": _capture_payload(stderr),
                    "stdout": _capture_payload(stdout),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )

    @staticmethod
    def _validate_argv(raw_argv: list[object]) -> tuple[str, ...]:
        if not raw_argv or len(raw_argv) > MAX_COMMAND_ARGUMENTS:
            raise RunCommandPreparationError(
                f"run_command argv must contain 1 to {MAX_COMMAND_ARGUMENTS} arguments"
            )

        argv: list[str] = []
        total_bytes = 0
        for index, value in enumerate(raw_argv):
            if not isinstance(value, str) or "\x00" in value:
                raise RunCommandPreparationError(
                    f"run_command argv[{index}] must be valid text without NUL"
                )
            if index == 0 and (not value or not value.strip()):
                raise RunCommandPreparationError(
                    "run_command argv[0] must name a nonblank executable"
                )
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError:
                raise RunCommandPreparationError(
                    f"run_command argv[{index}] must be valid UTF-8"
                ) from None
            if (
                len(value) > MAX_COMMAND_ARGUMENT_CHARACTERS
                or len(encoded) > MAX_COMMAND_ARGUMENT_BYTES
            ):
                raise RunCommandPreparationError(
                    f"run_command argv[{index}] exceeds {MAX_COMMAND_ARGUMENT_BYTES} bytes"
                )
            total_bytes += len(encoded)
            argv.append(value)
        if total_bytes > MAX_COMMAND_ARGV_BYTES:
            raise RunCommandPreparationError(
                f"run_command argv exceeds {MAX_COMMAND_ARGV_BYTES} total bytes"
            )
        return tuple(argv)

    def _validate_cwd(self, value: str) -> str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise RunCommandPreparationError("run_command cwd must be valid UTF-8") from None
        parts = value.split("/")
        invalid = (
            not value
            or not value.strip()
            or "\x00" in value
            or "\\" in value
            or value.startswith("/")
            or PureWindowsPath(value).drive
            or value != value.strip()
            or (value != "." and any(part in {"", ".", ".."} for part in parts))
            or value.endswith("/")
            or len(parts) > MAX_COMMAND_CWD_COMPONENTS
            or len(value) > MAX_COMMAND_CWD_CHARACTERS
            or len(encoded) > MAX_COMMAND_CWD_BYTES
        )
        if invalid:
            raise RunCommandPreparationError(
                "run_command cwd must be '.' or a portable workspace-relative directory"
            )

        self._inspect_directory(self._workspace, workspace_root=True)
        if value != ".":
            current = self._workspace
            for part in parts:
                current = current / part
                self._inspect_directory(current, workspace_root=False)
        return value

    @staticmethod
    def _inspect_directory(path: Path, *, workspace_root: bool) -> None:
        subject = "workspace root" if workspace_root else "cwd directory"
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise RunCommandPreparationError(f"run_command {subject} does not exist") from None
        except PermissionError:
            raise RunCommandPreparationError(f"run_command {subject} is not accessible") from None
        except OSError:
            raise RunCommandPreparationError(f"run_command could not inspect {subject}") from None
        if stat.S_ISLNK(info.st_mode):
            if workspace_root:
                raise RunCommandPreparationError(
                    "run_command workspace root must not be a symbolic link"
                )
            raise RunCommandPreparationError("run_command cwd must not contain a symbolic link")
        if not stat.S_ISDIR(info.st_mode):
            if workspace_root:
                raise RunCommandPreparationError(
                    "run_command workspace root must identify an existing directory"
                )
            raise RunCommandPreparationError("run_command cwd must identify an existing directory")


def _signal_process_group(
    process: subprocess.Popen[bytes], pgid: int, requested_signal: signal.Signals
) -> bool:
    try:
        os.killpg(pgid, requested_signal)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        if process.poll() is not None:
            return False
        try:
            process.send_signal(requested_signal)
        except OSError:
            return False
        return True


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes], pgid: int, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        process.poll()
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return process.poll() is not None
        except PermissionError:
            pass
        except OSError:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _drain_pipe(pipe, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            capture.consume(chunk)
    except (OSError, ValueError):
        capture.error = True


def _join_readers(readers: tuple[Thread, Thread], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.join(max(0.0, deadline - time.monotonic()))
    return all(not reader.is_alive() for reader in readers)


def _capture_payload(capture: _BoundedCapture) -> dict[str, object]:
    data = bytes(capture.captured)
    payload: dict[str, object] = {
        "bytes_captured": len(data),
        "bytes_total": capture.total,
        "truncated": capture.total > capture.limit,
    }
    try:
        payload["encoding"] = "utf-8"
        payload["text"] = data.decode("utf-8")
    except UnicodeDecodeError:
        payload["base64"] = base64.b64encode(data).decode("ascii")
        payload["encoding"] = "base64"
    return payload


def run_command_model_definition() -> dict[str, object]:
    """Return the exact provider-neutral controlled command definition."""
    return {
        "name": RUN_COMMAND_TOOL_NAME,
        "description": (
            "Run one bounded local command by direct argument vector in an existing workspace "
            "directory. This is dangerous full local process execution, not a shell or sandbox; "
            "the Host applies permission and approval policy, a fixed timeout, bounded output, "
            "and process-group cleanup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "description": "Direct executable and arguments; shell syntax is literal.",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_COMMAND_ARGUMENTS,
                },
                "cwd": {
                    "type": "string",
                    "description": "'.' or an existing portable workspace-relative directory.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Host-enforced timeout in whole seconds.",
                    "minimum": MIN_COMMAND_TIMEOUT_SECONDS,
                    "maximum": MAX_COMMAND_TIMEOUT_SECONDS,
                },
            },
            "required": ["argv", "cwd", "timeout_seconds"],
            "additionalProperties": False,
        },
    }


def run_command_tool_snapshot() -> CanonicalToolDefinition:
    return CanonicalToolDefinition.from_mapping(run_command_model_definition())
