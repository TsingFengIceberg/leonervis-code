from __future__ import annotations

import io
from pathlib import Path
from uuid import UUID

import pytest

from leonervis_code import __version__
from leonervis_code.cli.main import main
from leonervis_code.core.actions import ActionIdentity, ActionLease, ActionPrecondition
from leonervis_code.core.contracts import AssistantText, ToolArguments
from leonervis_code.core.permissions import (
    ApprovalMode,
    PermissionAction,
    PermissionGate,
    PermissionMode,
    PermissionRequest,
)
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.session_records import (
    ActionAuthorization,
    ActionExecutionOutcome,
    ApprovalAuditOutcome,
    BindingSnapshot,
    workspace_fingerprint,
)
from leonervis_code.session_store import SessionStore


class InteractiveStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_package_version_is_declared() -> None:
    assert __version__ == "0.1.0"


def test_prompt_command_runs_the_deterministic_foundation_loop(capsys, tmp_path) -> None:
    assert (
        main(
            ["prompt", "Hello"],
            cwd=tmp_path,
            environment={},
            user_profile_path=tmp_path / "user.json",
            project_profile_path=tmp_path / "project.json",
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == "Fake response: Hello\n"
    assert captured.err == ""


def test_session_list_marks_actual_latest_without_changing_creation_order(tmp_path) -> None:
    common = {
        "cwd": tmp_path,
        "environment": {},
        "user_profile_path": tmp_path / "user.json",
        "project_profile_path": tmp_path / "project.json",
    }
    empty = io.StringIO()
    assert main(["session", "list"], stdout=empty, stderr=io.StringIO(), **common) == 0
    assert empty.getvalue() == "No durable sessions found.\n"

    assert main(["prompt", "first"], stdout=io.StringIO(), stderr=io.StringIO(), **common) == 0
    shown = io.StringIO()
    assert main(["session", "show", "latest"], stdout=shown, stderr=io.StringIO(), **common) == 0
    first_id = next(
        line.removeprefix("session ID: ")
        for line in shown.getvalue().splitlines()
        if line.startswith("session ID: ")
    )

    assert main(["prompt", "second"], stdout=io.StringIO(), stderr=io.StringIO(), **common) == 0
    shown = io.StringIO()
    assert main(["session", "show", "latest"], stdout=shown, stderr=io.StringIO(), **common) == 0
    second_id = next(
        line.removeprefix("session ID: ")
        for line in shown.getvalue().splitlines()
        if line.startswith("session ID: ")
    )

    assert (
        main(
            ["--resume", first_id, "prompt", "resumed"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            **common,
        )
        == 0
    )
    output = io.StringIO()
    assert main(["session", "list"], stdout=output, stderr=io.StringIO(), **common) == 0

    lines = output.getvalue().splitlines()
    assert lines[0].startswith(f"{second_id}: 1 turn, closed, created ")
    assert lines[1].startswith(f"{first_id} [latest]: 2 turns, closed, created ")


def test_session_actions_replays_recent_redacted_action_audits(tmp_path) -> None:
    session_id = "12345678-1234-4234-9234-123456789abc"
    grant_id = "52345678-1234-4234-9234-123456789abc"
    store = SessionStore(
        tmp_path,
        uuid_factory=lambda: UUID(session_id),
        clock=lambda: "2026-07-23T12:00:00.000000Z",
    )
    binding = BindingSnapshot.fake()
    writer = store.create(binding)
    identity = ActionIdentity(
        request_id="32345678-1234-4234-9234-123456789abc",
        tool_use_id="write-1",
        tool_name="write_file",
        arguments=ToolArguments.from_mapping(
            {"content": "private-content-must-not-render", "path": "note.txt"}
        ),
        action=PermissionAction.WORKSPACE_CREATE,
        workspace_fingerprint=workspace_fingerprint(tmp_path),
        lease=ActionLease(
            session_id=session_id,
            lease_id="22345678-1234-4234-9234-123456789abc",
            runtime_generation=0,
            context_id="ctx-v1-" + "1" * 64,
        ),
        precondition=ActionPrecondition.path_absent(),
    )
    writer.action_requested(
        identity=identity,
        binding=binding,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
    )
    permission = PermissionGate().evaluate(
        PermissionRequest(
            PermissionMode.WORKSPACE_WRITE,
            ApprovalMode.ASK,
            PermissionAction.WORKSPACE_CREATE,
        )
    )
    writer.permission_decided(identity=identity, result=permission)
    writer.approval_resolved(
        identity=identity,
        outcome=ApprovalAuditOutcome.ACCEPTED,
        grant_id=grant_id,
    )
    writer.action_execution_started(
        identity=identity,
        authorization=ActionAuthorization.APPROVAL_GRANT,
        grant_id=grant_id,
    )
    writer.action_execution_finished(
        identity=identity,
        outcome=ActionExecutionOutcome.SUCCEEDED,
        result_code="created",
        message="private execution detail",
    )
    writer.close()

    output = io.StringIO()
    errors = io.StringIO()
    status = main(
        ["session", "actions", "latest", "--limit", "1"],
        cwd=tmp_path,
        stdout=output,
        stderr=errors,
        environment={},
        user_profile_path=tmp_path / "user.json",
        project_profile_path=tmp_path / "project.json",
    )

    assert status == 0
    assert errors.getvalue() == ""
    rendered = output.getvalue()
    assert "Action #1: write_file" in rendered
    assert "class: workspace-create" in rendered
    assert "path: 'note.txt'" in rendered
    assert "permission: ask (approval_required_workspace_create)" in rendered
    assert "approval: accepted" in rendered
    assert "result: succeeded (created)" in rendered
    assert "private-content-must-not-render" not in rendered
    assert "private execution detail" not in rendered
    assert grant_id not in rendered
    assert str(tmp_path) not in rendered


def test_session_actions_is_read_only_when_no_session_root_exists(tmp_path) -> None:
    output = io.StringIO()
    errors = io.StringIO()

    status = main(
        ["session", "actions", "latest"],
        cwd=tmp_path,
        stdout=output,
        stderr=errors,
        environment={},
        user_profile_path=tmp_path / "user.json",
        project_profile_path=tmp_path / "project.json",
    )

    assert status == 2
    assert output.getvalue() == ""
    assert "session directory does not exist or is inaccessible" in errors.getvalue()
    assert not (tmp_path / ".leonervis-code").exists()


def test_startup_resume_evidence_uses_stderr_and_stdout_remains_model_only(
    tmp_path,
) -> None:
    common = {
        "cwd": tmp_path,
        "environment": {},
        "user_profile_path": tmp_path / "user.json",
        "project_profile_path": tmp_path / "project.json",
    }
    assert main(["prompt", "first"], stdout=io.StringIO(), stderr=io.StringIO(), **common) == 0
    shown = io.StringIO()
    assert main(["session", "show", "latest"], stdout=shown, stderr=io.StringIO(), **common) == 0
    session_id = next(
        line.removeprefix("session ID: ")
        for line in shown.getvalue().splitlines()
        if line.startswith("session ID: ")
    )
    output = io.StringIO()
    errors = io.StringIO()

    status = main(
        ["--resume", session_id, "prompt", "second"],
        stdout=output,
        stderr=errors,
        **common,
    )

    assert status == 0
    assert output.getvalue() == "Fake response: second\n"
    assert "Resumed session" in errors.getvalue()
    assert "screening is unavailable for fake runtime" in errors.getvalue()
    assert "no provider request was made" in errors.getvalue()


def test_startup_resume_known_overflow_has_empty_stdout_and_does_not_mutate_target(
    tmp_path,
) -> None:
    user_path = tmp_path / "user.json"
    project_path = tmp_path / "project.json"
    common = {
        "cwd": tmp_path,
        "environment": {},
        "user_profile_path": user_path,
        "project_profile_path": project_path,
    }
    assert main(["prompt", "first"], stdout=io.StringIO(), stderr=io.StringIO(), **common) == 0
    shown = io.StringIO()
    assert main(["session", "show", "latest"], stdout=shown, stderr=io.StringIO(), **common) == 0
    values = dict(line.split(": ", 1) for line in shown.getvalue().splitlines() if ": " in line)
    session_id = values["session ID"]
    transcript = Path(values["transcript"])
    latest = transcript.parent / "latest.json"
    before_transcript = transcript.read_bytes()
    before_latest = latest.read_bytes()
    assert (
        main(
            [
                "provider",
                "add",
                "tiny",
                "--provider",
                "custom",
                "--model",
                "tiny-model",
                "--protocol",
                "openai-compatible",
                "--base-url",
                "http://127.0.0.1:11434/v1",
                "--context-window-tokens",
                "100",
                "--model-max-output-tokens",
                "4096",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            **common,
        )
        == 0
    )

    class CountingProvider:
        def count_input_tokens(self, request):
            from leonervis_code.providers.request_context import (
                RequestTokenCount,
                RequestTokenCountMethod,
            )

            return RequestTokenCount(1000, RequestTokenCountMethod.ESTIMATED)

        def respond(self, request):
            raise AssertionError("resume rejection must not invoke generation")

    output = io.StringIO()
    errors = io.StringIO()
    status = main(
        ["--profile", "tiny", "--resume", session_id, "prompt", "second"],
        stdout=output,
        stderr=errors,
        provider_factory=lambda route, *, environment: CountingProvider(),
        **common,
    )

    assert status == 2
    assert output.getvalue() == ""
    assert "Session resume rejected" in errors.getvalue()
    assert "No Session was resumed" in errors.getvalue()
    assert transcript.read_bytes() == before_transcript
    assert latest.read_bytes() == before_latest


def test_prompt_command_uses_its_cwd_as_the_read_file_workspace(monkeypatch, tmp_path) -> None:
    workspaces = []

    class RecordingReadFileTool:
        def __init__(self, workspace) -> None:
            workspaces.append(workspace)

    monkeypatch.setattr("leonervis_code.cli.main.ReadFileTool", RecordingReadFileTool)

    assert (
        main(
            ["prompt", "Hello"],
            cwd=tmp_path,
            environment={},
            user_profile_path=tmp_path / "user.json",
            project_profile_path=tmp_path / "project.json",
        )
        == 0
    )
    assert workspaces == [tmp_path.resolve()]


def test_real_prompt_requires_an_explicit_nonblank_model(capsys) -> None:
    with pytest.raises(SystemExit) as blank:
        main(["--model", "   ", "prompt", "Hello"])
    assert blank.value.code == 2
    assert "model must not be blank" in capsys.readouterr().err


def test_real_prompt_reports_missing_key_without_constructing_a_client(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    output = io.StringIO()
    errors = io.StringIO()

    assert (
        main(
            ["--model", "claude-opus-4-8", "prompt", "Hello"],
            stdout=output,
            stderr=errors,
            cwd=tmp_path,
        )
        == 2
    )

    assert output.getvalue() == ""
    assert errors.getvalue() == (
        "provider error [authentication]: ANTHROPIC_API_KEY is not configured\n"
    )


def test_real_prompt_uses_injected_provider_and_workspace(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-not-rendered")
    constructed = []

    class TextProvider:
        def respond(self, request):
            return AssistantText(text="Real provider response")

    def fake_factory(route, *, environment):
        constructed.append((route, dict(environment)))
        return TextProvider()

    monkeypatch.setattr("leonervis_code.cli.main.create_provider", fake_factory)
    output = io.StringIO()

    assert (
        main(
            ["--model", "claude-opus-4-8", "prompt", "Hello"],
            stdout=output,
            stderr=io.StringIO(),
            cwd=tmp_path,
        )
        == 0
    )

    assert output.getvalue() == "Real provider response\n"
    assert constructed[0][0].selected_model == "claude-opus-4-8"
    assert constructed[0][0].definition.provider_id == "anthropic"
    assert constructed[0][1]["ANTHROPIC_API_KEY"] == "secret-not-rendered"


def test_demo_read_visibly_executes_the_structured_tool_loop(tmp_path) -> None:
    (tmp_path / "README.md").write_text("workspace proof\n", encoding="utf-8")
    output = io.StringIO()

    assert main(["demo-read", "README.md"], stdout=output, cwd=tmp_path) == 0

    assert output.getvalue() == (
        "[demo] provider requested read_file: README.md\n"
        "[read_file] README.md\n"
        "  ✓ 16 UTF-8 bytes returned\n"
        "  preview: workspace proof\n"
        "Demo final response: provider received the read_file result.\n"
    )


def test_demo_read_visibly_reports_workspace_failures(tmp_path) -> None:
    output = io.StringIO()

    assert main(["demo-read", "../outside.txt"], stdout=output, cwd=tmp_path) == 0

    assert output.getvalue() == (
        "[demo] provider requested read_file: ../outside.txt\n"
        "[read_file] ../outside.txt\n"
        "  ✗ read_file path escapes the workspace\n"
        "Demo final response: provider received the read_file result.\n"
    )


def test_global_model_route_renders_real_provider_metadata_without_secret_values(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-must-not-render")
    output = io.StringIO()

    assert (
        main(
            ["--model", "openai/gpt-5", "route"],
            stdout=output,
            stderr=io.StringIO(),
        )
        == 0
    )

    assert output.getvalue() == (
        "provider: openai\n"
        "protocol: openai_chat_completions\n"
        "selected model: openai/gpt-5\n"
        "wire model: gpt-5\n"
        "base URL: https://api.openai.com/v1 (default)\n"
        "credential: configured\n"
        "context window: unknown (unknown)\n"
        "model max output: unknown (unknown)\n"
        "requested output reserve: 1024\n"
        "context diagnostic: live context discovery is unsupported\n"
    )
    assert "secret-must-not-render" not in output.getvalue()


def test_route_command_renders_the_offline_default_plan_without_secret_identifiers() -> None:
    output = io.StringIO()
    errors = io.StringIO()

    assert main(["route"], stdout=output, stderr=errors) == 0

    assert output.getvalue() == (
        "primary: fake-messages/alpha\n"
        "  credential: configured\n"
        "  canonical parameters: <none>\n"
        "  native preview: <none>\n"
        "  diagnostics: <none>\n"
    )
    assert errors.getvalue() == ""
    assert "foundation-2a-fake-messages" not in output.getvalue()


def test_route_command_compiles_provider_native_parameters_and_fallbacks() -> None:
    output = io.StringIO()

    assert (
        main(
            [
                "route",
                "--model",
                "beta",
                "--max-output-tokens",
                "32",
                "--fallback-model",
                "default",
            ],
            stdout=output,
            stderr=io.StringIO(),
        )
        == 0
    )

    assert output.getvalue() == (
        "primary: fake-chat/beta/1\n"
        "  credential: not configured\n"
        "  canonical parameters: max_output_tokens=32\n"
        "  native preview: max_output_tokens=32\n"
        "  diagnostics: <none>\n"
        "fallback: fake-messages/alpha\n"
        "  credential: configured\n"
        "  canonical parameters: max_output_tokens=32\n"
        "  native preview: max_tokens=32\n"
        "  diagnostics: <none>\n"
    )


def test_route_command_visibly_reports_known_soft_compatibility_adaptation() -> None:
    output = io.StringIO()
    errors = io.StringIO()

    assert (
        main(["route", "--model", "beta", "--temperature", "0.2"], stdout=output, stderr=errors)
        == 0
    )

    assert output.getvalue() == (
        "primary: fake-chat/beta/1\n"
        "  credential: not configured\n"
        "  canonical parameters: <none>\n"
        "  native preview: <none>\n"
        "  diagnostics:\n"
        "    info temperature_omitted_fixed_sampling: temperature is omitted for known "
        "fixed-sampling model fake-chat/beta/1 (omitted)\n"
    )
    assert errors.getvalue() == ""


def test_route_command_reports_hard_capability_errors_without_constructing_the_agent_loop() -> None:
    output = io.StringIO()
    errors = io.StringIO()

    assert (
        main(["route", "--model", "beta", "--require-streaming"], stdout=output, stderr=errors) == 2
    )

    assert output.getvalue() == ""
    assert errors.getvalue() == (
        "route error: model fake-chat/beta/1 lacks required capability: streaming\n"
    )


def test_bare_command_launches_the_interactive_terminal(tmp_path) -> None:
    stdout = InteractiveStream()

    status = main(
        [],
        stdin=InteractiveStream("Hello\n/exit\n"),
        stdout=stdout,
        stderr=io.StringIO(),
        cwd=tmp_path,
    )

    assert status == 0
    rendered = stdout.getvalue()
    assert "LEONERVIS CODE v0.1.0" in rendered
    assert "Fake response: Hello\n" in rendered


def test_bare_command_rejects_noninteractive_streams() -> None:
    error = io.StringIO()

    status = main([], stdin=io.StringIO(), stdout=io.StringIO(), stderr=error)

    assert status == 2
    assert error.getvalue() == (
        'interactive mode requires a terminal; use leonervis-code prompt "..." instead\n'
    )


def test_provider_profile_crud_and_active_precedence_use_injected_paths(tmp_path) -> None:
    user_path = tmp_path / "config" / "providers.json"
    project_path = tmp_path / "workspace" / "provider.json"
    output = io.StringIO()

    assert (
        main(
            [
                "provider",
                "add",
                "local-dev",
                "--provider",
                "custom",
                "--model",
                "Qwen/Qwen3.5",
                "--protocol",
                "openai-compatible",
                "--base-url",
                "http://127.0.0.1:11434",
            ],
            stdout=output,
            stderr=io.StringIO(),
            cwd=tmp_path,
            environment={},
            user_profile_path=user_path,
            project_profile_path=project_path,
        )
        == 0
    )
    assert output.getvalue() == "Saved provider profile local-dev.\n"

    constructed = []

    class LocalProvider:
        def respond(self, request):
            return AssistantText("local response")

    def factory(route, *, environment):
        constructed.append(route)
        return LocalProvider()

    output = io.StringIO()
    assert (
        main(
            ["provider", "use", "local-dev"],
            stdout=output,
            stderr=io.StringIO(),
            cwd=tmp_path,
            environment={},
            user_profile_path=user_path,
            project_profile_path=project_path,
            provider_factory=factory,
        )
        == 0
    )
    assert output.getvalue() == "Using provider profile local-dev at project scope.\n"

    output = io.StringIO()
    assert (
        main(
            ["prompt", "Hello"],
            stdout=output,
            stderr=io.StringIO(),
            cwd=tmp_path,
            environment={},
            user_profile_path=user_path,
            project_profile_path=project_path,
            provider_factory=factory,
        )
        == 0
    )
    assert output.getvalue() == "local response\n"
    assert constructed[-1].wire_model == "Qwen/Qwen3.5"

    output = io.StringIO()
    assert (
        main(
            ["provider", "list"],
            stdout=output,
            stderr=io.StringIO(),
            cwd=tmp_path,
            environment={},
            user_profile_path=user_path,
            project_profile_path=project_path,
        )
        == 0
    )
    assert output.getvalue() == "local-dev *: custom/Qwen/Qwen3.5\n"


def test_profile_model_override_is_runtime_only_and_profile_output_is_redacted(tmp_path) -> None:
    user_path = tmp_path / "providers.json"
    project_path = tmp_path / "project.json"
    common = {
        "cwd": tmp_path,
        "environment": {"VENDOR_KEY": "secret-must-not-render"},
        "user_profile_path": user_path,
        "project_profile_path": project_path,
    }
    assert (
        main(
            [
                "provider",
                "add",
                "vendor",
                "--provider",
                "custom",
                "--model",
                "default-model",
                "--protocol",
                "openai-compatible",
                "--base-url",
                "https://gateway.example/v1",
                "--api-key-env",
                "VENDOR_KEY",
                "--context-window-tokens",
                "131072",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            **common,
        )
        == 0
    )
    captured = []

    class TextProvider:
        def respond(self, request):
            return AssistantText("ok")

    def factory(route, *, environment):
        captured.append(route)
        return TextProvider()

    assert (
        main(
            ["--profile", "vendor", "--model", "temporary-model", "prompt", "Hi"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            provider_factory=factory,
            **common,
        )
        == 0
    )
    assert captured[0].selected_model == "temporary-model"

    output = io.StringIO()
    assert (
        main(
            ["provider", "show", "vendor"],
            stdout=output,
            stderr=io.StringIO(),
            **common,
        )
        == 0
    )
    rendered = output.getvalue()
    assert "profile ID:" in rendered
    assert "revision: 1" in rendered
    assert "model: default-model" in rendered
    assert "context window override: 131072" in rendered
    assert "credential: configured" in rendered
    assert "VENDOR_KEY" not in rendered
    assert "secret-must-not-render" not in rendered


def test_profile_identity_cli_supports_rename_replace_ids_and_migrate(tmp_path) -> None:
    user_path = tmp_path / "providers.json"
    project_path = tmp_path / "project.json"
    common = {
        "cwd": tmp_path,
        "environment": {},
        "user_profile_path": user_path,
        "project_profile_path": project_path,
    }
    assert (
        main(
            [
                "provider",
                "add",
                "local",
                "--provider",
                "custom",
                "--model",
                "one",
                "--protocol",
                "openai-compatible",
                "--base-url",
                "http://127.0.0.1:11434",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            **common,
        )
        == 0
    )
    store = ProviderProfileStore(user_path, project_path)
    profile = store.get_profile("local")

    output = io.StringIO()
    assert (
        main(
            ["provider", "rename", "--id", profile.profile_id, "renamed", "--if-revision", "1"],
            stdout=output,
            stderr=io.StringIO(),
            **common,
        )
        == 0
    )
    assert "Renamed provider profile local to renamed" in output.getvalue()

    output = io.StringIO()
    assert (
        main(
            [
                "provider",
                "replace",
                "renamed",
                "--provider",
                "custom",
                "--model",
                "two",
                "--protocol",
                "openai-compatible",
                "--base-url",
                "http://127.0.0.1:11434",
                "--if-revision",
                "2",
            ],
            stdout=output,
            stderr=io.StringIO(),
            **common,
        )
        == 0
    )
    assert "revision 3" in output.getvalue()

    output = io.StringIO()
    assert (
        main(["provider", "list", "--show-ids"], stdout=output, stderr=io.StringIO(), **common) == 0
    )
    assert profile.profile_id in output.getvalue()
    assert "r3" in output.getvalue()

    output = io.StringIO()
    assert main(["provider", "migrate"], stdout=output, stderr=io.StringIO(), **common) == 0
    assert output.getvalue() == "Migrated provider configuration to schema v4.\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["unknown"],
        ["prompt"],
        ["prompt", ""],
        ["prompt", "   "],
        ["session", "actions", "--limit", "0"],
        ["session", "actions", "--limit", "101"],
        ["session", "actions", "--limit", "two"],
        ["session", "actions", "--limit", "１０"],
    ],
)
def test_invalid_cli_input_exits_with_usage_error(arguments, capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(arguments)

    assert error.value.code == 2
    assert "usage: leonervis-code" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [(["--help"], "usage: leonervis-code"), (["prompt", "--help"], "the prompt to send")],
)
def test_help_exits_successfully(arguments, expected, capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(arguments)

    assert error.value.code == 0
    assert expected in capsys.readouterr().out


def test_version_exits_successfully(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out == "leonervis-code 0.1.0\n"
