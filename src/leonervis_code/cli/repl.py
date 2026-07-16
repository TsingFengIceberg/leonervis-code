"""Interactive terminal loop for a persistent project/provider session."""

from __future__ import annotations

from pathlib import Path
import readline
import sys
from typing import TextIO

from leonervis_code.cli.brand import render_banner
from leonervis_code.core.contracts import ConversationTurn
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.manager import RuntimeProviderStateError, RuntimeStatus
from leonervis_code.providers.profile import ProviderProfileError
from leonervis_code.providers.resolver import RuntimeRouteError

PROMPT = "leonervis> "
HELP_TEXT = (
    "Commands: /help, /history <count>, /exit, /quit. "
    "Provider controls: /status, /provider list, /provider current, "
    "/provider use <name>, /model <model>. Ctrl-D or Ctrl-C exits."
)
COMMANDS = (
    "/help",
    "/history",
    "/exit",
    "/quit",
    "/status",
    "/provider",
    "/model",
)


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


def render_runtime_status(status: RuntimeStatus) -> str:
    """Render redacted runtime status without credential names or values."""
    if status.mode == "fake":
        return "Mode: fake/offline\nProfile: <none>\nProvider: fake\nModel: <none>"
    credential = "not required"
    if status.credential_required:
        credential = "configured" if status.credential_present else "missing"
    return (
        f"Mode: real\n"
        f"Profile: {status.profile or '<direct>'} ({status.selection_source})\n"
        f"Provider: {status.provider_id} ({status.protocol})\n"
        f"Model: {status.selected_model}\n"
        f"Base URL: {status.base_url} ({status.base_url_source})\n"
        f"Credential: {credential}"
    )


def run_repl(
    session: object,
    *,
    stdin: TextIO,
    stdout: TextIO,
    version: str,
    cwd: Path,
    color: bool,
) -> int:
    """Run an in-memory multi-turn REPL until the user exits."""
    configure_tab_completion()
    stdout.write(f"\n{render_banner(version=version, cwd=cwd, color=color)}\n")
    status_method = getattr(session, "status", None)
    if callable(status_method):
        stdout.write(f"\n{render_runtime_status(status_method())}\n")
    stdout.write("\n")
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
                stdout.write(f"{render_recent_history(getattr(session, 'turns'), count)}\n")
            stdout.flush()
            continue
        if prompt == "/status":
            if not callable(status_method):
                stdout.write("Runtime status is unavailable.\n")
            else:
                stdout.write(f"{render_runtime_status(status_method())}\n")
            stdout.flush()
            continue
        if prompt == "/provider list":
            list_profiles = getattr(session, "list_profiles", None)
            if not callable(list_profiles):
                stdout.write("Provider profiles are unavailable.\n")
            else:
                profiles = list_profiles()
                if not profiles:
                    stdout.write("No provider profiles configured.\n")
                else:
                    for profile in profiles:
                        stdout.write(f"{profile.name}: {profile.provider_id}/{profile.model}\n")
            stdout.flush()
            continue
        if prompt == "/provider current":
            if not callable(status_method):
                stdout.write("Runtime status is unavailable.\n")
            else:
                stdout.write(f"{render_runtime_status(status_method())}\n")
            stdout.flush()
            continue
        if prompt == "/provider use" or prompt.startswith("/provider use "):
            parts = prompt.split()
            if len(parts) != 3:
                stdout.write("Usage: /provider use <name>\n")
            else:
                try:
                    updated = getattr(session, "use_profile")(parts[2], scope="project")
                    stdout.write(f"Using provider profile {updated.profile} for this workspace.\n")
                except (
                    ProviderAdapterError,
                    ProviderProfileError,
                    RuntimeProviderStateError,
                    RuntimeRouteError,
                ) as error:
                    stdout.write(f"Provider switch failed: {_safe_error(error)}\n")
            stdout.flush()
            continue
        if prompt == "/model" or prompt.startswith("/model "):
            parts = prompt.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                stdout.write("Usage: /model <model>\n")
            else:
                try:
                    updated = getattr(session, "set_model")(parts[1])
                    stdout.write(
                        f"Runtime model changed to {updated.selected_model}; profile was not modified.\n"
                    )
                except (
                    ProviderAdapterError,
                    ProviderProfileError,
                    RuntimeProviderStateError,
                    RuntimeRouteError,
                ) as error:
                    stdout.write(f"Model switch failed: {_safe_error(error)}\n")
            stdout.flush()
            continue
        if prompt.startswith("/"):
            stdout.write(f"Unknown command: {prompt}. Type /help for controls.\n")
            stdout.flush()
            continue

        try:
            prompt_method = getattr(session, "prompt", None)
            if callable(prompt_method):
                response = prompt_method(prompt)
            else:
                response = getattr(session, "run")(prompt)
            stdout.write(f"{response}\n")
        except ProviderAdapterError as error:
            stdout.write(f"Provider error [{error.failure.kind}]: {error.failure.message}\n")
        except (ProviderProfileError, RuntimeProviderStateError) as error:
            stdout.write(f"Runtime error: {error}\n")
        stdout.flush()


def _safe_error(error: Exception) -> str:
    if isinstance(error, ProviderAdapterError):
        return error.failure.message
    return str(error)
