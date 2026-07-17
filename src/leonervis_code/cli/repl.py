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
    "Commands: /help, /history <count>, /session show, /session list, /session new, "
    "/resume <latest|id>, /exit, /quit. Provider controls: /status, "
    "/provider list, /provider current, /provider use <name>, /model <model>. "
    "Ctrl-D or Ctrl-C exits."
)
COMMANDS = (
    "/help",
    "/history",
    "/exit",
    "/quit",
    "/status",
    "/provider",
    "/model",
    "/session",
    "/resume",
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


def render_session_summary(
    info,
    *,
    current_session_id: str | None = None,
    latest_session_id: str | None = None,
) -> str:
    """Render compact Session metadata with explicit pointer markers."""
    markers = []
    if info.session_id == current_session_id:
        markers.append("[current]")
    if info.session_id == latest_session_id:
        markers.append("[latest]")
    marker_text = f" {' '.join(markers)}" if markers else ""
    turns = f"{info.turn_count} {'turn' if info.turn_count == 1 else 'turns'}"
    state = "closed" if info.closed else "open"
    return f"{info.session_id}{marker_text}: {turns}, {state}, created {info.created_at}"


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


def render_session_info(info) -> str:
    """Render one durable Session without exposing transcript contents."""
    return (
        f"Session: {info.session_id}\n"
        f"Transcript: {info.path}\n"
        f"Turns: {info.turn_count}\n"
        f"Created: {info.created_at}"
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
    session_info_method = getattr(session, "session_info", None)
    if callable(session_info_method):
        stdout.write(f"\n{render_session_info(session_info_method())}\nAuto-save: enabled\n")
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
        if prompt == "/session show":
            if not callable(session_info_method):
                stdout.write("Durable session information is unavailable.\n")
            else:
                stdout.write(f"{render_session_info(session_info_method())}\n")
            stdout.flush()
            continue
        if prompt == "/session list":
            list_sessions = getattr(session, "list_sessions", None)
            if not callable(list_sessions):
                stdout.write("Durable sessions are unavailable.\n")
            else:
                sessions = list_sessions()
                if not sessions:
                    stdout.write("No durable sessions found.\n")
                current_id = (
                    session_info_method().session_id if callable(session_info_method) else None
                )
                latest_session_info = getattr(session, "latest_session_info", None)
                latest_id = (
                    latest_session_info().session_id if callable(latest_session_info) else None
                )
                for info in sessions:
                    stdout.write(
                        f"{render_session_summary(info, current_session_id=current_id, latest_session_id=latest_id)}\n"
                    )
            stdout.flush()
            continue
        if prompt == "/session new":
            new_session = getattr(session, "new_session", None)
            if not callable(new_session):
                stdout.write("Creating durable sessions is unavailable.\n")
            else:
                try:
                    info = new_session()
                    stdout.write(
                        f"Started new session {info.session_id}; runtime provider unchanged.\n"
                    )
                except Exception as error:
                    stdout.write(f"Session creation failed: {_safe_error(error)}\n")
            stdout.flush()
            continue
        if prompt == "/resume" or prompt.startswith("/resume "):
            parts = prompt.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                stdout.write("Usage: /resume <latest|session-id>\n")
            else:
                switch = getattr(session, "switch_session", None)
                if not callable(switch):
                    stdout.write("Session switching is unavailable.\n")
                else:
                    try:
                        info = switch(parts[1].strip())
                        stdout.write(
                            f"Resumed session {info.session_id}; runtime provider unchanged.\n"
                        )
                    except Exception as error:
                        stdout.write(f"Session resume failed: {_safe_error(error)}\n")
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
