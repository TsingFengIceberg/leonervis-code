"""Pure terminal presentation for the Leonervis Code CLI."""

from __future__ import annotations

import re
from typing import Literal, Protocol

RESET = "\x1b[0m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
BOLD = "\x1b[1m"
_READLINE_START = "\001"
_READLINE_END = "\002"
_RUNTIME_WIDTH = 24
_SAFE_PROMPT_CHARACTER = re.compile(r"[A-Za-z0-9._:-]")

MessageKind = Literal["plain", "info", "success", "warning", "error"]

HELP_TEXT = (
    "Commands: /help, /history <count>, /session, /provider, /status, /model <model>, "
    "/resume <latest|id>, /exit, /quit. Ctrl-D or Ctrl-C exits."
)
SESSION_HELP = (
    "Session commands:\n"
    "  /session show\n"
    "  /session list\n"
    "  /session new\n"
    "  /resume <latest|session-id>"
)
PROVIDER_HELP = (
    "Provider commands:\n"
    "  /provider list\n"
    "  /provider current\n"
    "  /provider use <name>\n"
    "  /status\n"
    "  /model <model>"
)


class RuntimeStatusView(Protocol):
    mode: str
    profile: str | None
    selection_source: str
    provider_id: str
    protocol: str | None
    selected_model: str | None
    base_url: str | None
    base_url_source: str | None
    credential_required: bool
    credential_present: bool
    context_window_tokens: int | None
    context_window_source: str
    context_window_diagnostic: str | None


class SessionInfoView(Protocol):
    session_id: str
    path: object
    turn_count: int
    created_at: str
    closed: bool


class ConversationTurnView(Protocol):
    user: object
    assistant: object


def render_prompt(
    status: RuntimeStatusView | None,
    session: SessionInfoView | None,
    *,
    color: bool,
    readline: bool = False,
) -> str:
    """Render a compact prompt from redacted public runtime snapshots."""
    session_label = _session_label(session)
    runtime_label = _runtime_label(status)
    fields = [field for field in (session_label, runtime_label) if field is not None]
    if not fields:
        return "leonervis> "

    if not color:
        return f"leonervis[{'|'.join(fields)}]> "

    styled_fields = []
    if session_label is not None:
        styled_fields.append(_ansi(session_label, BLUE, readline=readline))
    if runtime_label is not None:
        runtime_color = YELLOW if status is not None and status.mode == "fake" else BLUE
        styled_fields.append(_ansi(runtime_label, runtime_color, readline=readline))
    brand = _ansi("leonervis", BOLD, readline=readline)
    arrow = _ansi(">", GREEN, readline=readline)
    return f"{brand}[{'|'.join(styled_fields)}]{arrow} "


def render_message(text: str, kind: MessageKind, *, color: bool) -> str:
    """Apply a traditional semantic color without changing message text."""
    if not color or kind == "plain":
        return text
    code = {
        "info": BLUE,
        "success": GREEN,
        "warning": YELLOW,
        "error": RED,
    }[kind]
    return f"{code}{text}{RESET}"


def render_recent_history(turns: tuple[ConversationTurnView, ...], count: int) -> str:
    """Render the most recent complete conversation turns in chronological order."""
    recent_turns = turns[-count:]
    if not recent_turns:
        return "No conversation turns yet."
    return "\n\n".join(
        f"User: {turn.user.text}\nAssistant: {turn.assistant.text}" for turn in recent_turns
    )


def render_session_summary(
    info: SessionInfoView,
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


def render_runtime_status(status: RuntimeStatusView) -> str:
    """Render redacted runtime status without credential names or values."""
    if status.mode == "fake":
        return "Mode: fake/offline\nProfile: <none>\nProvider: fake\nModel: <none>"
    credential = "not required"
    if status.credential_required:
        credential = "configured" if status.credential_present else "missing"
    context = (
        f"{status.context_window_tokens} tokens ({status.context_window_source})"
        if status.context_window_tokens is not None
        else "unknown"
    )
    diagnostic = (
        f"\nContext diagnostic: {status.context_window_diagnostic}"
        if status.context_window_diagnostic
        else ""
    )
    return (
        f"Mode: real\n"
        f"Profile: {status.profile or '<direct>'} ({status.selection_source})\n"
        f"Provider: {status.provider_id} ({status.protocol})\n"
        f"Model: {status.selected_model}\n"
        f"Base URL: {status.base_url} ({status.base_url_source})\n"
        f"Credential: {credential}\n"
        f"Context window: {context}{diagnostic}"
    )


def render_session_info(info: SessionInfoView) -> str:
    """Render one durable Session without exposing transcript contents."""
    return (
        f"Session: {info.session_id}\n"
        f"Transcript: {info.path}\n"
        f"Turns: {info.turn_count}\n"
        f"Created: {info.created_at}"
    )


def _session_label(info: SessionInfoView | None) -> str | None:
    if info is None:
        return None
    value = str(info.session_id)
    if len(value) < 8:
        return "unknown"
    prefix = value[:8]
    return prefix if all(character in "0123456789abcdef" for character in prefix) else "unknown"


def _runtime_label(status: RuntimeStatusView | None) -> str | None:
    if status is None:
        return None
    if status.mode == "fake":
        return "fake"
    if status.mode == "real" and status.profile:
        raw = status.profile
    elif status.mode == "real":
        raw = f"direct:{status.provider_id}"
    else:
        raw = "unknown"
    return _truncate(_safe_prompt_text(raw), _RUNTIME_WIDTH)


def _safe_prompt_text(value: object) -> str:
    projected = "".join(
        character if character.isascii() and _SAFE_PROMPT_CHARACTER.fullmatch(character) else "?"
        for character in str(value)
    )
    return projected or "unknown"


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return f"{value[: width - 3]}..."


def _ansi(text: str, code: str, *, readline: bool) -> str:
    if not readline:
        return f"{code}{text}{RESET}"
    return f"{_READLINE_START}{code}{_READLINE_END}{text}{_READLINE_START}{RESET}{_READLINE_END}"
