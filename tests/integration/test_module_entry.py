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
