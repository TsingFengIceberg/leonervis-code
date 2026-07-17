from __future__ import annotations

import os
import subprocess
import sys


def isolated_environment(tmp_path):
    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    return environment


def test_module_entry_runs_one_deterministic_prompt_turn(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "prompt", "Hello"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=isolated_environment(tmp_path),
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == "Fake response: Hello\n"
    assert result.stderr == ""


def test_module_entry_visibly_demonstrates_a_read_file_tool_loop(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("manual proof\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "demo-read", "note.txt"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=isolated_environment(tmp_path),
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == (
        "[demo] provider requested read_file: note.txt\n"
        "[read_file] note.txt\n"
        "  ✓ 13 UTF-8 bytes returned\n"
        "  preview: manual proof\n"
        "Demo final response: provider received the read_file result.\n"
    )
    assert result.stderr == ""


def test_module_entry_renders_a_deterministic_offline_route_plan(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "leonervis_code",
            "route",
            "--model",
            "beta",
            "--max-output-tokens",
            "32",
        ],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=isolated_environment(tmp_path),
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == (
        "primary: fake-chat/beta/1\n"
        "  credential: not configured\n"
        "  canonical parameters: max_output_tokens=32\n"
        "  native preview: max_output_tokens=32\n"
        "  diagnostics: <none>\n"
    )
    assert result.stderr == ""


def test_module_entry_resumes_durable_session_across_processes(tmp_path) -> None:
    environment = isolated_environment(tmp_path)
    first = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "prompt", "first"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert first.returncode == 0
    assert first.stdout == "Fake response: first\n"

    shown = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "session", "show", "latest"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert shown.returncode == 0
    session_id = next(
        line.removeprefix("session ID: ")
        for line in shown.stdout.splitlines()
        if line.startswith("session ID: ")
    )
    assert "turns: 1" in shown.stdout

    resumed = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "--resume", session_id, "prompt", "second"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert resumed.returncode == 0
    assert resumed.stdout == "Fake response: second\n"

    listed = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "session", "list"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert listed.returncode == 0
    assert f"{session_id}: 2 turns" in listed.stdout


def test_bare_module_entry_requires_an_interactive_terminal(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "leonervis_code"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=isolated_environment(tmp_path),
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        'interactive mode requires a terminal; use leonervis-code prompt "..." instead\n'
    )
