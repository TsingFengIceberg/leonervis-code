"""Pure terminal presentation for the Leonervis Code CLI."""

from __future__ import annotations

import re
from typing import Literal, Protocol

from leonervis_code.providers.request_context import ContextFitDecision, ContextFitReport

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
    "Commands: /help, /history <count>, /session, /provider, /status, /context, /compact, "
    "/model <model>, /resume <latest|id>, /exit, /quit. Ctrl-D or Ctrl-C exits."
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
    model_max_output_tokens: int | None
    model_max_output_source: str
    model_max_output_diagnostic: str | None
    max_output_tokens: int | None


class EffectiveContextInspectionView(Protocol):
    source: str
    context_id: str
    full_turn_count: int
    full_item_count: int
    effective_turn_count: int
    summary_present: bool
    retained_turn_count: int
    latest_checkpoint_sequence: int | None
    latest_checkpoint_trigger: object | None
    fit_report: ContextFitReport | None
    fit_decision: ContextFitDecision
    remaining_capacity: int | None
    target_assessment: object


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
    model_output = (
        f"{status.model_max_output_tokens} tokens ({status.model_max_output_source})"
        if status.model_max_output_tokens is not None
        else "unknown"
    )
    output_reserve = (
        f"{status.max_output_tokens} tokens" if status.max_output_tokens is not None else "unknown"
    )
    diagnostic = (
        f"\nContext diagnostic: {status.context_window_diagnostic}"
        if status.context_window_diagnostic
        else ""
    )
    output_diagnostic = (
        f"\nModel output diagnostic: {status.model_max_output_diagnostic}"
        if status.model_max_output_diagnostic
        else ""
    )
    return (
        f"Mode: real\n"
        f"Profile: {status.profile or '<direct>'} ({status.selection_source})\n"
        f"Provider: {status.provider_id} ({status.protocol})\n"
        f"Model: {status.selected_model}\n"
        f"Base URL: {status.base_url} ({status.base_url_source})\n"
        f"Credential: {credential}\n"
        f"Context window: {context}{diagnostic}\n"
        f"Model max output: {model_output}{output_diagnostic}\n"
        f"Requested output reserve: {output_reserve}"
    )


def render_context_inspection(
    inspection: EffectiveContextInspectionView,
) -> tuple[str, MessageKind]:
    """Render approved context metadata without exposing model-visible content."""
    report = inspection.fit_report
    source = inspection.source.replace("_", " ")
    lines = [
        f"Source: {source}",
        f"Context ID: {inspection.context_id}",
        f"Full history: {_count_label(inspection.full_turn_count, 'turn')}, "
        f"{_count_label(inspection.full_item_count, 'item')}",
        f"Effective history: {_count_label(inspection.effective_turn_count, 'turn')}, "
        f"{_count_label(inspection.effective_item_count, 'item')}",
        f"Compact summary: {'present' if inspection.summary_present else 'absent'}",
    ]
    if inspection.summary_present:
        lines.append(
            f"Retained real history: {_count_label(inspection.retained_turn_count, 'turn')}"
        )
        if inspection.latest_checkpoint_sequence is not None:
            lines.append(f"Checkpoint sequence: {inspection.latest_checkpoint_sequence}")
        if inspection.latest_checkpoint_trigger is not None:
            lines.append(
                f"Checkpoint trigger: {inspection.latest_checkpoint_trigger.value.replace('_', ' ')}"
            )
    diagnostic = None
    if report is None:
        lines.extend(
            (
                "Input: unavailable",
                "Output reserve: unavailable",
                "Context window: unknown",
                "Model max output: unknown",
                "Fit: unknown",
            )
        )
        diagnostic = getattr(
            inspection.target_assessment,
            "unavailable_diagnostic",
            None,
        )
        kind: MessageKind = "warning"
    else:
        count = report.input_count
        if count.input_tokens is None:
            lines.append("Input: unknown")
        else:
            lines.append(f"Input: {count.input_tokens} tokens ({count.method.value})")
        lines.append(f"Output reserve: {report.requested_output_tokens} tokens")
        lines.append(
            f"Context window: {report.context_window_limit} tokens"
            if report.context_window_limit is not None
            else "Context window: unknown"
        )
        lines.append(
            f"Model max output: {report.model_output_limit} tokens"
            if report.model_output_limit is not None
            else "Model max output: unknown"
        )
        lines.append(f"Fit: {report.decision.value}")
        if inspection.remaining_capacity is not None:
            lines.append(f"Remaining capacity: {inspection.remaining_capacity} tokens")
        diagnostic = count.diagnostic
        if report.decision == ContextFitDecision.FITS:
            kind = "info"
        elif report.decision == ContextFitDecision.UNKNOWN:
            kind = "warning"
        else:
            kind = "error"
    if diagnostic:
        lines.append(f"Diagnostic: {diagnostic}")
    return "\n".join(lines), kind


def render_session_resume(result: object) -> tuple[str, MessageKind]:
    """Render target-aware resume evidence and any applied pointer warning."""
    if result.effect.value == "already_current":
        return (
            f"Session {result.session_id} is already current; no resume record was written.",
            "info",
        )
    report = result.fit_report
    prefix = f"Resumed session {result.session_id}; current runtime unchanged."
    if report is None:
        message = (
            f"{prefix} Compatibility screening is unavailable for fake runtime, "
            "and no provider request was made."
        )
        kind: MessageKind = "warning"
    elif report.decision == ContextFitDecision.FITS:
        message = (
            f"{prefix} Committed context fits: input={report.input_count.input_tokens} "
            f"({report.input_count.method.value}) + reserve={report.requested_output_tokens} "
            f"<= window={report.context_window_limit}. The next provider invocation "
            "still runs full preflight."
        )
        kind = "success"
    else:
        diagnostic = report.input_count.diagnostic or "required context facts are unknown"
        message = (
            f"{prefix} Compatibility was not confirmed: {diagnostic}. The resume was "
            "applied, no history was deleted, and the next provider invocation will "
            "run full preflight."
        )
        kind = "warning"
    if result.effect.value == "applied_latest_failed":
        message += " The resume audit is durable, but latest pointer update failed."
        kind = "error"
    elif result.effect.value == "applied_latest_durability_unknown":
        message += " The latest pointer was replaced, but crash durability is unconfirmed."
        kind = "warning"
    if result.recovery_applied:
        message += " An incomplete crash tail was recovered during commit."
    return message, kind


def render_resume_rejection(report: ContextFitReport, *, startup: bool = False) -> str:
    """Render a known-incompatible resume with truthful unchanged state."""
    if report.decision == ContextFitDecision.MODEL_OUTPUT_EXCEEDED:
        detail = (
            f"reserve={report.requested_output_tokens} > model max output="
            f"{report.model_output_limit}"
        )
    else:
        detail = (
            f"input={report.input_count.input_tokens} ({report.input_count.method.value}) + "
            f"reserve={report.requested_output_tokens} > window={report.context_window_limit}"
        )
    state = (
        "No Session was resumed; the latest pointer and runtime selection are unchanged."
        if startup
        else "Current Session, latest pointer, target transcript, and runtime are unchanged."
    )
    return f"Session resume rejected: {detail}. {state}"


def render_prompt_event(event: object) -> tuple[str, MessageKind]:
    """Render one safe automatic-compaction lifecycle event."""
    name = type(event).__name__
    trigger = event.trigger.value.replace("_", " ")
    if name == "AutoCompactionStarted":
        threshold = (
            f" at the {event.high_water_percent}% high-water mark"
            if event.high_water_percent is not None
            else " after known context overflow"
        )
        return (
            f"Automatic compact started{threshold}: input={event.input_tokens} "
            f"({event.input_method}) + reserve={event.requested_output_tokens}, "
            f"window={event.context_window_tokens}; trigger={trigger}.",
            "info",
        )
    if name == "AutoCompactionCommitted":
        result = event.result
        return (
            f"Automatic compact committed ({trigger}): summarized "
            f"{result.summarized_turn_count} complete turns, retained "
            f"{result.retained_turn_count}; input {result.before_input_tokens} -> "
            f"{result.after_input_tokens} ({result.input_method}); checkpoint "
            f"{result.checkpoint_sequence}. Full transcript and /history were preserved.",
            "success",
        )
    continuation = (
        "the original prompt will continue"
        if event.prompt_continues
        else ("the original prompt will not be sent")
    )
    return (
        f"Automatic compact was not applied ({trigger}): {event.reason}; {continuation}.",
        "warning" if event.prompt_continues else "error",
    )


def render_compact_result(result: object) -> str:
    """Render one committed checkpoint without exposing summary contents."""
    return (
        f"Compacted {result.summarized_turn_count} complete turns; retained "
        f"{result.retained_turn_count} turns.\n"
        f"Context ID: {result.source_context_id} -> {result.result_context_id}\n"
        f"Input: {result.before_input_tokens} -> {result.after_input_tokens} tokens "
        f"({result.input_method}); fit: {result.fit_decision.value}.\n"
        f"Checkpoint: sequence {result.checkpoint_sequence} in session "
        f"{result.session_id}. Full transcript and /history were preserved."
    )


def render_runtime_switch(
    destination: str,
    report: ContextFitReport | None,
    *,
    suffix: str,
) -> tuple[str, MessageKind]:
    """Render committed switch evidence without claiming more than the probe proved."""
    if report is None:
        return f"{destination}; {suffix}", "success"
    if report.decision == ContextFitDecision.FITS:
        return (
            f"{destination}; committed context fits: input="
            f"{report.input_count.input_tokens} ({report.input_count.method.value}) + "
            f"reserve={report.requested_output_tokens} <= window="
            f"{report.context_window_limit}. The next provider invocation still runs "
            f"full preflight; {suffix}",
            "success",
        )
    diagnostic = report.input_count.diagnostic or "required context facts are unknown"
    return (
        f"{destination}; compatibility not confirmed: {diagnostic}. "
        "The switch was applied, no history was deleted, and the next provider "
        f"invocation will run full preflight; {suffix}",
        "warning",
    )


def render_switch_rejection(report: ContextFitReport) -> str:
    """Render a safe known-overflow rejection with explicit unchanged state."""
    if report.decision == ContextFitDecision.MODEL_OUTPUT_EXCEEDED:
        detail = (
            f"reserve={report.requested_output_tokens} > model max output="
            f"{report.model_output_limit}"
        )
    else:
        detail = (
            f"input={report.input_count.input_tokens} "
            f"({report.input_count.method.value}) + reserve="
            f"{report.requested_output_tokens} > window={report.context_window_limit}"
        )
    return (
        f"Runtime switch rejected: {detail}. Current runtime and profile selection "
        "are unchanged. Keep the current runtime or use /session new before switching."
    )


def render_session_info(info: SessionInfoView) -> str:
    """Render one durable Session without exposing transcript contents."""
    return (
        f"Session: {info.session_id}\n"
        f"Transcript: {info.path}\n"
        f"Turns: {info.turn_count}\n"
        f"Created: {info.created_at}"
    )


def _count_label(value: int, label: str) -> str:
    suffix = label if value == 1 else f"{label}s"
    return f"{value} {suffix}"


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
