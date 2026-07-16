from __future__ import annotations

import io

import pytest

from leonervis_code import __version__
from leonervis_code.cli.main import main


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
