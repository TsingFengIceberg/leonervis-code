"""Interactive terminal loop for the deterministic Foundation 0 slice."""

from __future__ import annotations

from pathlib import Path
import readline
import sys
from typing import TextIO

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.cli.brand import render_banner
from leonervis_code.core.contracts import ConversationTurn

PROMPT = "leonervis> "
HELP_TEXT = "Commands: /help, /history <count>, /exit, /quit. Ctrl-D or Ctrl-C exits."
COMMANDS = ("/help", "/history", "/exit", "/quit")


def complete_command(text: str, state: int) -> str | None:
    """Return the next slash-command completion for readline."""
    matches = [command for command in COMMANDS if command.startswith(text)]
    return matches[state] if state < len(matches) else None


def configure_tab_completion() -> None:
    """Configure readline to complete the REPL's supported slash commands."""
    readline.set_completer(complete_command)
    readline.set_completer_delims(readline.get_completer_delims().replace("/", ""))
    readline.parse_and_bind("tab: complete")


def read_prompt(stdin: TextIO, stdout: TextIO) -> str:
    """Read one prompt, enabling readline editing for the real terminal streams."""
    if stdin is sys.stdin and stdout is sys.stdout:
        return input(PROMPT)
    stdout.write(PROMPT)
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


def render_recent_history(turns: tuple[ConversationTurn, ...], count: int) -> str:
    """Render the most recent complete conversation turns in chronological order."""
    recent_turns = turns[-count:]
    if not recent_turns:
        return "No conversation turns yet."
    return "\n\n".join(
        f"User: {turn.user.text}\nAssistant: {turn.assistant.text}" for turn in recent_turns
    )


def run_repl(
    loop: AgentLoop,
    *,
    stdin: TextIO,
    stdout: TextIO,
    version: str,
    cwd: Path,
    color: bool,
) -> int:
    """Run the in-memory deterministic REPL until the user exits."""
    configure_tab_completion()
    stdout.write(f"\n{render_banner(version=version, cwd=cwd, color=color)}\n\n")
    stdout.flush()

    while True:
        try:
            prompt = read_prompt(stdin, stdout)
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
        if prompt in {"/exit", "/quit"}:
            return 0
        if prompt == "/help":
            stdout.write(f"{HELP_TEXT}\n")
            stdout.flush()
            continue
        if prompt.startswith("/history"):
            count = parse_history_count(prompt)
            if count is None:
                stdout.write("Usage: /history <positive integer>\n")
            else:
                stdout.write(f"{render_recent_history(loop.turns, count)}\n")
            stdout.flush()
            continue
        if prompt.startswith("/"):
            stdout.write(f"Unknown command: {prompt}. Type /help for controls.\n")
            stdout.flush()
            continue

        stdout.write(f"{loop.run(prompt)}\n")
        stdout.flush()
