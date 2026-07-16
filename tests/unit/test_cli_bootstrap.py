from __future__ import annotations

import io

import pytest

from leonervis_code import __version__
from leonervis_code.cli.main import main
from leonervis_code.core.contracts import AssistantText


class InteractiveStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_package_version_is_declared() -> None:
    assert __version__ == "0.1.0"


def test_prompt_command_runs_the_deterministic_foundation_loop(capsys) -> None:
    assert main(["prompt", "Hello"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "Fake response: Hello\n"
    assert captured.err == ""


def test_prompt_command_uses_its_cwd_as_the_read_file_workspace(monkeypatch, tmp_path) -> None:
    workspaces = []

    class RecordingReadFileTool:
        def __init__(self, workspace) -> None:
            workspaces.append(workspace)

    monkeypatch.setattr("leonervis_code.cli.main.ReadFileTool", RecordingReadFileTool)

    assert main(["prompt", "Hello"], cwd=tmp_path) == 0
    assert workspaces == [tmp_path]


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
        def respond(self, history):
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


@pytest.mark.parametrize(
    "arguments",
    [["unknown"], ["prompt"], ["prompt", ""], ["prompt", "   "]],
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
