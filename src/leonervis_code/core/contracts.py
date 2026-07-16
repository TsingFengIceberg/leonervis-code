"""Structured contracts shared by the Foundation 1B tool-loop slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias


@dataclass(frozen=True)
class UserMessage:
    """One user text input in an ordered in-memory conversation."""

    text: str


@dataclass(frozen=True)
class AssistantText:
    """The final visible assistant text for one completed conversation turn."""

    text: str


@dataclass(frozen=True)
class ToolUse:
    """One provider-requested read-only tool action."""

    tool_use_id: str
    name: str
    path: str


@dataclass(frozen=True)
class ToolResult:
    """One host-produced result corresponding to a ``ToolUse`` request."""

    tool_use_id: str
    content: str
    is_error: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ConversationTurn:
    """One completed user/final-assistant pair for REPL history display."""

    user: UserMessage
    assistant: AssistantText


ConversationItem: TypeAlias = UserMessage | AssistantText | ToolUse | ToolResult
ProviderResponse: TypeAlias = AssistantText | ToolUse


class ConversationProvider(Protocol):
    """Produce one structured assistant response from ordered conversation context."""

    def respond(self, history: tuple[ConversationItem, ...]) -> ProviderResponse:
        """Return final assistant text or one requested tool action."""
