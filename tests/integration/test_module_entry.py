from __future__ import annotations

import subprocess
import sys


def test_module_entry_runs_one_deterministic_prompt_turn() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "prompt", "Hello"],
        capture_output=True,
        check=False,
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


def test_bare_module_entry_requires_an_interactive_terminal() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "leonervis_code"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        'interactive mode requires a terminal; use leonervis-code prompt "..." instead\n'
    )
