from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from leonervis_code.core.actions import ActionIdentity, ActionLease, ActionPrecondition
from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.core.permissions import (
    ApprovalMode,
    PermissionAction,
    PermissionDecision,
    PermissionGate,
    PermissionMode,
    PermissionReason,
    PermissionRequest,
    PermissionResult,
)
from leonervis_code.tools.run_command import (
    COMMAND_ENVIRONMENT_ALLOWLIST,
    MAX_COMMAND_ARGUMENTS,
    MAX_COMMAND_ARGUMENT_BYTES,
    MAX_COMMAND_ARGV_BYTES,
    MAX_COMMAND_CWD_BYTES,
    MAX_COMMAND_CWD_COMPONENTS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MIN_COMMAND_TIMEOUT_SECONDS,
    RUN_COMMAND_TOOL_NAME,
    PreparedRunCommand,
    RunCommandPreparationError,
    RunCommandTool,
)

SESSION_ID = "12345678-1234-4234-9234-123456789abc"
LEASE_ID = "22345678-1234-4234-9234-123456789abc"
REQUEST_ID = "32345678-1234-4234-9234-123456789abc"
CONTEXT_ID = f"ctx-v1-{'1' * 64}"
WORKSPACE_FINGERPRINT = f"v1-{'2' * 64}"


def request(
    *,
    argv: object | None = None,
    cwd: object = ".",
    timeout_seconds: object = 60,
    name: str = RUN_COMMAND_TOOL_NAME,
) -> ToolUse:
    return ToolUse(
        "command-1",
        name,
        ToolArguments.from_mapping(
            {
                "argv": ["uv", "run", "pytest"] if argv is None else argv,
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
            }
        ),
    )


def identity(prepared: PreparedRunCommand) -> ActionIdentity:
    return ActionIdentity(
        request_id=REQUEST_ID,
        tool_use_id=prepared.request.tool_use_id,
        tool_name=prepared.request.name,
        arguments=prepared.request.arguments,
        action=prepared.action,
        workspace_fingerprint=WORKSPACE_FINGERPRINT,
        lease=ActionLease(SESSION_ID, LEASE_ID, 7, CONTEXT_ID),
        precondition=prepared.precondition,
    )


def test_prepare_freezes_exact_command_without_starting_a_process(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    prepared = RunCommandTool(tmp_path).prepare(
        request(
            argv=["uv", "run", "pytest", "-q", "tests"],
            cwd="tests",
            timeout_seconds=90,
        )
    )

    assert prepared == PreparedRunCommand(
        request=prepared.request,
        argv=("uv", "run", "pytest", "-q", "tests"),
        relative_cwd="tests",
        timeout_seconds=90,
        action=PermissionAction.DANGEROUS,
        precondition=ActionPrecondition.none(),
    )
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    with pytest.raises(FrozenInstanceError):
        prepared.timeout_seconds = 1


def test_prepare_accepts_literal_shell_metacharacters_without_interpreting_them(
    tmp_path: Path,
) -> None:
    prepared = RunCommandTool(tmp_path).prepare(
        request(argv=["printf", "%s", "a | b", "$(not-executed)", "", "*.py"])
    )

    assert prepared.argv == (
        "printf",
        "%s",
        "a | b",
        "$(not-executed)",
        "",
        "*.py",
    )


def test_future_executor_environment_allowlist_is_closed_and_excludes_provider_keys() -> None:
    assert COMMAND_ENVIRONMENT_ALLOWLIST == (
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
    assert "PWD" not in COMMAND_ENVIRONMENT_ALLOWLIST
    assert "ANTHROPIC_API_KEY" not in COMMAND_ENVIRONMENT_ALLOWLIST
    assert "OPENAI_API_KEY" not in COMMAND_ENVIRONMENT_ALLOWLIST


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": ["uv"], "cwd": "."},
        {"argv": ["uv"], "cwd": ".", "timeout_seconds": 60, "extra": True},
        {"argv": "uv", "cwd": ".", "timeout_seconds": 60},
        {"argv": ["uv"], "cwd": 1, "timeout_seconds": 60},
    ],
)
def test_prepare_rejects_nonclosed_or_malformed_input(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    malformed = ToolUse(
        "command-1",
        RUN_COMMAND_TOOL_NAME,
        ToolArguments.from_mapping(arguments),
    )

    with pytest.raises(RunCommandPreparationError, match="input is malformed"):
        RunCommandTool(tmp_path).prepare(malformed)


def test_prepare_rejects_wrong_tool_name(tmp_path: Path) -> None:
    with pytest.raises(RunCommandPreparationError, match="input is malformed"):
        RunCommandTool(tmp_path).prepare(request(name="write_file"))


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "1 to"),
        (["uv"] * (MAX_COMMAND_ARGUMENTS + 1), "1 to"),
        ([""], "nonblank executable"),
        (["   "], "nonblank executable"),
        (["uv", 1], r"argv\[1\]"),
        (["uv", "bad\x00argument"], r"argv\[1\]"),
        (["x" * (MAX_COMMAND_ARGUMENT_BYTES + 1)], "exceeds"),
        (["你" * ((MAX_COMMAND_ARGUMENT_BYTES // 3) + 1)], "exceeds"),
    ],
)
def test_prepare_rejects_invalid_or_oversized_argv(
    tmp_path: Path, argv: list[object], message: str
) -> None:
    with pytest.raises(RunCommandPreparationError, match=message):
        RunCommandTool(tmp_path).prepare(request(argv=argv))


def test_prepare_rejects_argv_over_total_byte_limit(tmp_path: Path) -> None:
    argv = ["x" * 512] * ((MAX_COMMAND_ARGV_BYTES // 512) + 1)

    with pytest.raises(RunCommandPreparationError, match="total bytes"):
        RunCommandTool(tmp_path).prepare(request(argv=argv))


def test_prepare_accepts_argv_count_and_timeout_boundaries(tmp_path: Path) -> None:
    tool = RunCommandTool(tmp_path)

    assert len(tool.prepare(request(argv=["x"] * MAX_COMMAND_ARGUMENTS)).argv) == 64
    for timeout in (MIN_COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_TIMEOUT_SECONDS):
        assert tool.prepare(request(timeout_seconds=timeout)).timeout_seconds == timeout


@pytest.mark.parametrize("timeout", [True, False, 0, -1, 1.5, MAX_COMMAND_TIMEOUT_SECONDS + 1])
def test_prepare_rejects_invalid_timeout(tmp_path: Path, timeout: object) -> None:
    with pytest.raises(RunCommandPreparationError, match="timeout_seconds"):
        RunCommandTool(tmp_path).prepare(request(timeout_seconds=timeout))


def test_prepare_accepts_workspace_root_and_existing_nested_directory(tmp_path: Path) -> None:
    (tmp_path / "packages" / "core").mkdir(parents=True)
    tool = RunCommandTool(tmp_path)

    assert tool.prepare(request(cwd=".")).relative_cwd == "."
    assert tool.prepare(request(cwd="packages/core")).relative_cwd == "packages/core"


@pytest.mark.parametrize(
    "cwd",
    [
        "",
        "   ",
        "/tmp",
        "C:/tmp",
        "packages\\core",
        "./packages",
        "packages/.",
        "packages/../core",
        "packages//core",
        "packages/",
        "bad\x00cwd",
        "x" * (MAX_COMMAND_CWD_BYTES + 1),
        "你" * ((MAX_COMMAND_CWD_BYTES // 3) + 1),
        "/".join(["x"] * (MAX_COMMAND_CWD_COMPONENTS + 1)),
    ],
)
def test_prepare_rejects_nonportable_cwd(tmp_path: Path, cwd: str) -> None:
    with pytest.raises(RunCommandPreparationError, match="portable workspace-relative"):
        RunCommandTool(tmp_path).prepare(request(cwd=cwd))


def test_prepare_rejects_missing_file_and_symlink_cwd_components(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "nested").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "real" / "nested-link").symlink_to(
        tmp_path / "real" / "nested", target_is_directory=True
    )
    (tmp_path / "broken").symlink_to(tmp_path / "missing", target_is_directory=True)
    tool = RunCommandTool(tmp_path)

    with pytest.raises(RunCommandPreparationError, match="does not exist"):
        tool.prepare(request(cwd="missing"))
    with pytest.raises(RunCommandPreparationError, match="existing directory"):
        tool.prepare(request(cwd="file.txt"))
    for cwd in ("link", "link/nested", "real/nested-link", "broken"):
        with pytest.raises(RunCommandPreparationError, match="symbolic link"):
            tool.prepare(request(cwd=cwd))


def test_revalidate_rejects_cwd_changed_to_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    tool = RunCommandTool(tmp_path)
    prepared = tool.prepare(request(cwd="target"))
    target.rmdir()
    target.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(RunCommandPreparationError, match="symbolic link"):
        tool.revalidate(prepared)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("missing", "workspace root does not exist"),
        ("file", "workspace root must identify an existing directory"),
        ("symlink", "workspace root must not be a symbolic link"),
    ],
)
def test_revalidate_rejects_replaced_workspace_root(
    tmp_path: Path,
    replacement: str,
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = RunCommandTool(workspace)
    prepared = tool.prepare(request(cwd="."))
    workspace.rmdir()
    if replacement == "file":
        workspace.write_text("not a directory", encoding="utf-8")
    elif replacement == "symlink":
        workspace.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(RunCommandPreparationError, match=message):
        tool.revalidate(prepared)


def test_prepared_command_reuses_dangerous_permission_policy() -> None:
    prepared = PreparedRunCommand(
        request=request(),
        argv=("uv", "run", "pytest"),
        relative_cwd=".",
        timeout_seconds=60,
        action=PermissionAction.DANGEROUS,
        precondition=ActionPrecondition.none(),
    )
    gate = PermissionGate()

    for approval_mode in ApprovalMode:
        assert gate.evaluate(
            PermissionRequest(PermissionMode.READ_ONLY, approval_mode, prepared.action)
        ) == PermissionResult(
            PermissionDecision.DENY,
            PermissionReason.DENIED_READ_ONLY_MODE,
        )
        assert gate.evaluate(
            PermissionRequest(PermissionMode.WORKSPACE_WRITE, approval_mode, prepared.action)
        ) == PermissionResult(
            PermissionDecision.DENY,
            PermissionReason.DENIED_WORKSPACE_WRITE_MODE,
        )
    assert gate.evaluate(
        PermissionRequest(
            PermissionMode.DANGER_FULL_ACCESS,
            ApprovalMode.ASK,
            prepared.action,
        )
    ) == PermissionResult(
        PermissionDecision.ASK,
        PermissionReason.APPROVAL_REQUIRED_DANGEROUS,
    )
    assert gate.evaluate(
        PermissionRequest(
            PermissionMode.DANGER_FULL_ACCESS,
            ApprovalMode.AUTO,
            prepared.action,
        )
    ) == PermissionResult(
        PermissionDecision.ALLOW,
        PermissionReason.ALLOWED_DANGEROUS_AUTO,
    )


def test_action_identity_binds_exact_argv_cwd_timeout_lease_and_context(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    tool = RunCommandTool(tmp_path)
    approved = identity(
        tool.prepare(request(argv=["uv", "run", "pytest", "-q"], cwd="tests", timeout_seconds=60))
    )

    assert ActionIdentity.from_mapping(approved.as_mapping()) == approved
    variants = [
        identity(
            tool.prepare(
                request(
                    argv=["uv", "run", "pytest", "tests/unit"],
                    cwd="tests",
                    timeout_seconds=60,
                )
            )
        ),
        identity(
            tool.prepare(request(argv=["uv", "run", "pytest", "-q"], cwd=".", timeout_seconds=60))
        ),
        identity(
            tool.prepare(
                request(argv=["uv", "run", "pytest", "-q"], cwd="tests", timeout_seconds=61)
            )
        ),
        replace(
            approved,
            lease=replace(approved.lease, runtime_generation=8),
        ),
        replace(
            approved,
            lease=replace(approved.lease, context_id=f"ctx-v1-{'3' * 64}"),
        ),
    ]

    assert all(candidate.digest != approved.digest for candidate in variants)
