"""Interactive terminal orchestration for a persistent project session."""

from __future__ import annotations

from pathlib import Path
import readline
import sys
from typing import TextIO

from leonervis_code.cli.brand import render_banner
from leonervis_code.cli.presentation import (
    render_message,
    render_prompt,
    render_runtime_status,
    render_session_info,
)
from leonervis_code.cli.slash import TOP_LEVEL_COMMANDS, dispatch_slash
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.manager import RuntimeProviderStateError
from leonervis_code.providers.profile import ProviderProfileError
from leonervis_code.providers.request_context import ContextPreflightError

PLAIN_PROMPT = "leonervis> "


def complete_command(text: str, state: int) -> str | None:
    """Return the next top-level slash-command completion for readline."""
    matches = [command for command in TOP_LEVEL_COMMANDS if command.startswith(text)]
    return matches[state] if state < len(matches) else None


def configure_tab_completion() -> None:
    """Configure readline to complete the REPL's supported slash commands."""
    readline.set_completer(complete_command)
    readline.set_completer_delims(readline.get_completer_delims().replace("/", ""))
    readline.parse_and_bind("tab: complete")


def read_prompt(stdin: TextIO, stdout: TextIO, prompt: str = PLAIN_PROMPT) -> str:
    """Read one prompt, enabling readline editing for the real terminal streams."""
    if stdin is sys.stdin and stdout is sys.stdout:
        return input(prompt)
    stdout.write(prompt)
    stdout.flush()
    line = stdin.readline()
    if line == "":
        raise EOFError
    return line.rstrip("\r\n")


def parse_history_count(command: str) -> int | None:
    """Return a positive count from ``/history <count>``, if valid."""
    parts = command.split()
    if (
        len(parts) != 2
        or parts[0] != "/history"
        or not parts[1].isascii()
        or not parts[1].isdigit()
    ):
        return None
    count = int(parts[1])
    return count if count > 0 else None


def run_repl(
    session: object,
    *,
    stdin: TextIO,
    stdout: TextIO,
    version: str,
    cwd: Path,
    color: bool,
) -> int:
    """Read input, dispatch local commands, and route ordinary text to the model."""
    configure_tab_completion()
    stdout.write(f"\n{render_banner(version=version, cwd=cwd, color=color)}\n")
    status = _snapshot(session, "status")
    if status is not None:
        stdout.write(f"\n{render_runtime_status(status)}\n")
    session_info = _snapshot(session, "session_info")
    if session_info is not None:
        stdout.write(f"\n{render_session_info(session_info)}\nAuto-save: enabled\n")
    stdout.write("\n")
    stdout.flush()

    while True:
        status = _snapshot(session, "status")
        session_info = _snapshot(session, "session_info")
        real_readline = stdin is sys.stdin and stdout is sys.stdout
        prompt_text = render_prompt(
            status,
            session_info,
            color=color,
            readline=real_readline,
        )
        try:
            prompt = read_prompt(stdin, stdout, prompt_text)
        except KeyboardInterrupt:
            stdout.write("\n")
            stdout.flush()
            return 0
        except EOFError:
            stdout.write("\n")
            stdout.flush()
            return 0

        if not prompt.strip():
            continue

        result = dispatch_slash(prompt, session)
        if result.handled:
            if result.message is not None:
                stdout.write(f"{render_message(result.message, result.kind, color=color)}\n")
                stdout.flush()
            if result.exit:
                return 0
            continue

        try:
            prompt_method = getattr(session, "prompt", None)
            if callable(prompt_method):
                response = prompt_method(prompt)
            else:
                response = getattr(session, "run")(prompt)
            stdout.write(f"{response}\n")
        except ContextPreflightError as error:
            message = f"Context preflight error: {error}"
            stdout.write(f"{render_message(message, 'error', color=color)}\n")
        except ProviderAdapterError as error:
            message = f"Provider error [{error.failure.kind}]: {error.failure.message}"
            stdout.write(f"{render_message(message, 'error', color=color)}\n")
        except (ProviderProfileError, RuntimeProviderStateError) as error:
            stdout.write(f"{render_message(f'Runtime error: {error}', 'error', color=color)}\n")
        stdout.flush()


def _snapshot(session: object, method_name: str):
    method = getattr(session, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None
