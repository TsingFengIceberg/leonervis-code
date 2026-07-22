"""Slash-command dispatch independent from terminal streams and ANSI rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leonervis_code.cli.presentation import (
    HELP_TEXT,
    PROVIDER_HELP,
    SESSION_HELP,
    MessageKind,
    render_compact_result,
    render_context_inspection,
    render_recent_history,
    render_runtime_status,
    render_runtime_switch,
    render_resume_rejection,
    render_session_info,
    render_session_resume,
    render_session_summary,
    render_switch_rejection,
)
from leonervis_code.core.compaction import CompactionError
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.manager import (
    RuntimeProviderStateError,
    RuntimeSwitchAuditError,
    RuntimeSwitchContextError,
)
from leonervis_code.providers.profile import ProviderProfileError
from leonervis_code.providers.resolver import RuntimeRouteError
from leonervis_code.session import SessionResumeConflictError, SessionResumeContextError
from leonervis_code.session_store import SessionResumeCommitError, SessionStoreError

TOP_LEVEL_COMMANDS = (
    "/help",
    "/history",
    "/exit",
    "/quit",
    "/status",
    "/context",
    "/compact",
    "/provider",
    "/model",
    "/session",
    "/resume",
)


class ReplSession(Protocol):
    turns: tuple

    def status(self): ...

    def inspect_context(self): ...

    def compact_context(self): ...

    def session_info(self): ...

    def latest_session_info(self): ...

    def list_sessions(self): ...

    def new_session(self): ...

    def switch_session(self, selector: str): ...

    def list_profiles(self): ...

    def use_profile(self, name: str, *, scope: str): ...

    def set_model(self, model: str): ...


@dataclass(frozen=True)
class SlashResult:
    """One stream-independent result from slash-command dispatch."""

    handled: bool
    exit: bool = False
    message: str | None = None
    kind: MessageKind = "plain"


_NOT_HANDLED = SlashResult(handled=False)


def dispatch_slash(command: str, session: ReplSession) -> SlashResult:
    """Dispatch one exact slash command without writing terminal output."""
    if not command.startswith("/"):
        return _NOT_HANDLED
    if command in {"/exit", "/quit"}:
        return SlashResult(handled=True, exit=True)
    if command.startswith("/exit ") or command.startswith("/quit "):
        name = command.split(maxsplit=1)[0]
        return _usage(f"Usage: {name}")
    if command == "/help":
        return _info(HELP_TEXT)
    if command.startswith("/help "):
        return _usage("Usage: /help")
    if command == "/session":
        return _info(SESSION_HELP)
    if command == "/provider":
        return _info(PROVIDER_HELP)
    if command == "/status":
        return _call(lambda: render_runtime_status(session.status()), kind="info")
    if command.startswith("/status "):
        return _usage("Usage: /status")
    if command == "/context":
        try:
            message, kind = render_context_inspection(session.inspect_context())
            return SlashResult(handled=True, message=message, kind=kind)
        except Exception as error:
            return _command_error(error, failure_prefix="Context inspection failed")
    if command.startswith("/context "):
        return _usage("Usage: /context")
    if command == "/compact":
        try:
            return SlashResult(
                handled=True,
                message=render_compact_result(session.compact_context()),
                kind="success",
            )
        except Exception as error:
            result = _command_error(error, failure_prefix="Compaction failed")
            suffix = " Full history and effective context are unchanged."
            return SlashResult(
                handled=True,
                message=f"{result.message}{suffix}",
                kind=result.kind,
            )
    if command.startswith("/compact "):
        return _usage("Usage: /compact")
    if command == "/history" or command.startswith("/history "):
        return _history(command, session)
    if command == "/session show" or command.startswith("/session show "):
        if command != "/session show":
            return _usage("Usage: /session show")
        return _call(lambda: render_session_info(session.session_info()), kind="info")
    if command == "/session list" or command.startswith("/session list "):
        if command != "/session list":
            return _usage("Usage: /session list")
        return _session_list(session)
    if command == "/session new" or command.startswith("/session new "):
        if command != "/session new":
            return _usage("Usage: /session new")
        return _new_session(session)
    if command.startswith("/session "):
        subcommand = command.split(maxsplit=2)[1]
        return _usage(f"Unknown session command: {subcommand}\nUsage: /session <show|list|new>")
    if command == "/resume" or command.startswith("/resume "):
        return _resume(command, session)
    if command == "/provider list" or command.startswith("/provider list "):
        if command != "/provider list":
            return _usage("Usage: /provider list")
        return _provider_list(session)
    if command == "/provider current" or command.startswith("/provider current "):
        if command != "/provider current":
            return _usage("Usage: /provider current")
        return _call(lambda: render_runtime_status(session.status()), kind="info")
    if command == "/provider use" or command.startswith("/provider use "):
        return _provider_use(command, session)
    if command.startswith("/provider "):
        subcommand = command.split(maxsplit=2)[1]
        return _usage(
            f"Unknown provider command: {subcommand}\nUsage: /provider <list|current|use>"
        )
    if command == "/model" or command.startswith("/model "):
        return _model(command, session)
    return _usage(f"Unknown command: {command}. Type /help for controls.")


def _history(command: str, session: ReplSession) -> SlashResult:
    parts = command.split()
    if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit() or int(parts[1]) <= 0:
        return _usage("Usage: /history <positive integer>")
    return _call(lambda: render_recent_history(session.turns, int(parts[1])))


def _session_list(session: ReplSession) -> SlashResult:
    def render() -> str:
        sessions = session.list_sessions()
        if not sessions:
            return "No durable sessions found."
        current_id = session.session_info().session_id
        latest_id = session.latest_session_info().session_id
        return "\n".join(
            render_session_summary(
                info,
                current_session_id=current_id,
                latest_session_id=latest_id,
            )
            for info in sessions
        )

    return _call(render, kind="info")


def _new_session(session: ReplSession) -> SlashResult:
    return _call(
        lambda: (
            f"Started new session {session.new_session().session_id}; runtime provider unchanged."
        ),
        kind="success",
        failure_prefix="Session creation failed",
    )


def _resume(command: str, session: ReplSession) -> SlashResult:
    parts = command.split()
    if len(parts) != 2:
        return _usage("Usage: /resume <latest|session-id>")
    try:
        message, kind = render_session_resume(session.switch_session(parts[1]))
        return SlashResult(handled=True, message=message, kind=kind)
    except SessionResumeContextError as error:
        return SlashResult(
            handled=True,
            message=render_resume_rejection(error.report),
            kind="error",
        )
    except SessionResumeConflictError as error:
        return SlashResult(
            handled=True,
            message=(
                f"Session resume was not committed: {error}. Current Session and runtime "
                f"are unchanged. Retry /resume {parts[1]}."
            ),
            kind="warning",
        )
    except SessionResumeCommitError as error:
        return SlashResult(
            handled=True,
            message=(
                f"Session resume commit failed at {error.stage.value}: {error}. "
                "Inspect the target transcript before retrying."
            ),
            kind="error",
        )
    except Exception as error:
        return _command_error(error, failure_prefix="Session resume failed")


def _provider_list(session: ReplSession) -> SlashResult:
    def render() -> str:
        profiles = session.list_profiles()
        if not profiles:
            return "No provider profiles configured."
        return "\n".join(
            f"{profile.name}: {profile.provider_id}/{profile.model}" for profile in profiles
        )

    return _call(render, kind="info")


def _provider_use(command: str, session: ReplSession) -> SlashResult:
    parts = command.split()
    if len(parts) != 3:
        return _usage("Usage: /provider use <name>")
    try:
        result = session.use_profile(parts[2], scope="project")
        message, kind = render_runtime_switch(
            f"Using provider profile {result.status.profile} for this workspace",
            result.fit_report,
            suffix="active workspace selection updated",
        )
        return SlashResult(handled=True, message=message, kind=kind)
    except RuntimeSwitchContextError as error:
        return SlashResult(
            handled=True,
            message=render_switch_rejection(error.report),
            kind="error",
        )
    except RuntimeSwitchAuditError as error:
        return SlashResult(
            handled=True,
            message=(
                "Runtime changed, but Session audit persistence failed. "
                f"Effective profile: {error.result.status.profile or '<direct>'}."
            ),
            kind="error",
        )
    except Exception as error:
        return _command_error(error, failure_prefix="Provider switch failed")


def _model(command: str, session: ReplSession) -> SlashResult:
    parts = command.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return _usage("Usage: /model <model>")
    model = parts[1].strip()
    try:
        result = session.set_model(model)
        message, kind = render_runtime_switch(
            f"Runtime model changed to {result.status.selected_model}",
            result.fit_report,
            suffix="profile was not modified",
        )
        return SlashResult(handled=True, message=message, kind=kind)
    except RuntimeSwitchContextError as error:
        return SlashResult(
            handled=True,
            message=render_switch_rejection(error.report),
            kind="error",
        )
    except RuntimeSwitchAuditError as error:
        return SlashResult(
            handled=True,
            message=(
                "Runtime changed, but Session audit persistence failed. "
                f"Effective model: {error.result.status.selected_model}."
            ),
            kind="error",
        )
    except Exception as error:
        return _command_error(error, failure_prefix="Model switch failed")


def _command_error(error: Exception, *, failure_prefix: str) -> SlashResult:
    if isinstance(error, ProviderAdapterError):
        message = error.failure.message
    elif isinstance(
        error,
        (
            CompactionError,
            ProviderProfileError,
            RuntimeProviderStateError,
            RuntimeRouteError,
            SessionStoreError,
        ),
    ):
        message = str(error)
    else:
        message = "unexpected internal error"
    return SlashResult(
        handled=True,
        message=f"{failure_prefix}: {message}",
        kind="error",
    )


def _call(
    operation,
    *,
    kind: MessageKind = "plain",
    failure_prefix: str = "Command failed",
) -> SlashResult:
    try:
        return SlashResult(handled=True, message=operation(), kind=kind)
    except Exception as error:
        return _command_error(error, failure_prefix=failure_prefix)


def _usage(message: str) -> SlashResult:
    return SlashResult(handled=True, message=message, kind="warning")


def _info(message: str) -> SlashResult:
    return SlashResult(handled=True, message=message, kind="info")
