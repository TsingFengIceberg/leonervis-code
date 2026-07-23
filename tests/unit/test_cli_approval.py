from __future__ import annotations

import io
from pathlib import Path

import pytest

from leonervis_code.cli.main import main, terminal_approval_handler
from leonervis_code.cli.repl import run_repl
from leonervis_code.core.action_coordinator import ApprovalResolution, HumanApprovalRequest
from leonervis_code.core.actions import ActionIdentity, ActionLease, ActionPrecondition
from leonervis_code.core.contracts import AssistantText, ToolArguments, ToolUse
from leonervis_code.core.permissions import (
    ApprovalMode,
    PermissionAction,
    PermissionMode,
    PermissionDecision,
    PermissionReason,
    PermissionResult,
)
from leonervis_code.providers.request_context import RequestTokenCount, RequestTokenCountMethod
from leonervis_code.session import ProjectSession


class ToolProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def count_input_tokens(self, _request):
        return RequestTokenCount(100, RequestTokenCountMethod.ESTIMATED)

    def respond(self, request):
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1:
            return ToolUse(
                "write-1",
                "write_file",
                ToolArguments.from_mapping(
                    {"path": "note.txt", "content": "secret model content\n"}
                ),
            )
        return AssistantText("finished")


class NoReadInput(io.StringIO):
    def readline(self, *_args, **_kwargs):
        raise AssertionError("one-shot approval must not read stdin")


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def common(tmp_path: Path, provider: ToolProvider) -> dict:
    return {
        "cwd": tmp_path,
        "environment": {},
        "user_profile_path": tmp_path / "user.json",
        "project_profile_path": tmp_path / "project.json",
        "provider_factory": lambda route, *, environment: provider,
    }


def real_args(*tail: str) -> list[str]:
    return [
        "--model",
        "custom/model",
        "--provider-protocol",
        "openai-compatible",
        "--base-url",
        "http://127.0.0.1:11434/v1",
        *tail,
    ]


def approval_request() -> HumanApprovalRequest:
    identity = ActionIdentity(
        request_id="12345678-1234-4234-9234-123456789abc",
        tool_use_id="write-1",
        tool_name="write_file",
        arguments=ToolArguments.from_mapping(
            {"path": "note.txt", "content": "secret model content\n"}
        ),
        action=PermissionAction.WORKSPACE_CREATE,
        workspace_fingerprint=f"v1-{'1' * 64}",
        lease=ActionLease(
            "22345678-1234-4234-9234-123456789abc",
            "32345678-1234-4234-9234-123456789abc",
            0,
            f"ctx-v1-{'2' * 64}",
        ),
        precondition=ActionPrecondition.path_absent(),
    )
    return HumanApprovalRequest(
        identity,
        PermissionResult(
            PermissionDecision.ASK,
            PermissionReason.APPROVAL_REQUIRED_WORKSPACE_CREATE,
        ),
    )


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("y\n", ApprovalResolution.ACCEPT),
        ("yes\n", ApprovalResolution.ACCEPT),
        ("\n", ApprovalResolution.REJECT),
        ("n\n", ApprovalResolution.REJECT),
        ("no\n", ApprovalResolution.REJECT),
        ("c\n", ApprovalResolution.CANCEL),
        ("cancel\n", ApprovalResolution.CANCEL),
        ("", ApprovalResolution.CANCEL),
        ("bad\nwrong\nmaybe\n", ApprovalResolution.CANCEL),
    ],
)
def test_terminal_approval_handler_has_bounded_explicit_resolutions(
    input_text: str, expected: ApprovalResolution
) -> None:
    stdin = io.StringIO(input_text)
    stdout = io.StringIO()

    resolution = terminal_approval_handler(stdin, stdout)(approval_request())

    assert resolution == expected
    presentation = stdout.getvalue()
    assert "workspace-create write_file" in presentation
    assert "path='note.txt'" in presentation
    assert "bytes=21" in presentation
    assert "secret model content" not in presentation
    assert presentation.count("Please answer") <= 3


def test_terminal_approval_keyboard_interrupt_cancels() -> None:
    class InterruptingInput(io.StringIO):
        def readline(self, *_args, **_kwargs):
            raise KeyboardInterrupt

    stdout = io.StringIO()

    assert (
        terminal_approval_handler(InterruptingInput(), stdout)(approval_request())
        == ApprovalResolution.CANCEL
    )
    assert stdout.getvalue().endswith("\n")


def test_one_shot_ask_never_reads_stdin_and_cancels_without_writing(tmp_path: Path) -> None:
    provider = ToolProvider()
    stdout = io.StringIO()
    stderr = io.StringIO()

    status = main(
        real_args(
            "--permission-mode",
            "workspace-write",
            "--approval",
            "ask",
            "prompt",
            "write it",
        ),
        stdin=NoReadInput(),
        stdout=stdout,
        stderr=stderr,
        **common(tmp_path, provider),
    )

    assert status == 0
    assert stdout.getvalue() == "finished\n"
    assert "Approval required" not in stdout.getvalue()
    assert not (tmp_path / "note.txt").exists()
    assert provider.requests[1].history[-1].content == "action approval cancelled"


def test_one_shot_auto_requires_explicit_write_capability_and_executes(tmp_path: Path) -> None:
    provider = ToolProvider()
    stdout = io.StringIO()
    stderr = io.StringIO()

    status = main(
        real_args(
            "--permission-mode",
            "workspace-write",
            "--approval",
            "auto",
            "prompt",
            "write it",
        ),
        stdin=NoReadInput(),
        stdout=stdout,
        stderr=stderr,
        **common(tmp_path, provider),
    )

    assert status == 0
    assert stdout.getvalue() == "finished\n"
    assert stderr.getvalue() == ""
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "secret model content\n"


def test_repl_ask_uses_terminal_confirmation_and_does_not_echo_content(tmp_path: Path) -> None:
    provider = ToolProvider()
    stdin = TtyStringIO("write it\ny\n/exit\n")
    stdout = TtyStringIO()
    session = ProjectSession.open(
        tmp_path,
        model="custom/model",
        custom_protocol="openai-compatible",
        custom_base_url="http://127.0.0.1:11434/v1",
        environment={},
        provider_factory=lambda route, *, environment: provider,
        user_profile_path=tmp_path / "user.json",
        project_profile_path=tmp_path / "project.json",
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=terminal_approval_handler(stdin, stdout),
    )
    try:
        status = run_repl(
            session,
            stdin=stdin,
            stdout=stdout,
            version="0.test",
            cwd=tmp_path,
            color=False,
        )
    finally:
        session.close()

    assert status == 0
    rendered = stdout.getvalue()
    assert "Approval required: workspace-create write_file path='note.txt' bytes=21" in rendered
    assert "secret model content" not in rendered
    assert "finished" in rendered
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "secret model content\n"


def test_invalid_permission_and_approval_flags_are_argparse_errors(capsys) -> None:
    with pytest.raises(SystemExit) as permission:
        main(["--permission-mode", "root", "prompt", "hello"])
    assert permission.value.code == 2
    assert "invalid choice" in capsys.readouterr().err

    with pytest.raises(SystemExit) as approval:
        main(["--approval", "always", "prompt", "hello"])
    assert approval.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
