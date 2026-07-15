"""Core contracts shared by the first Harness learning slices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class TextMessage:
    """One ordered text message in an in-memory conversation."""

    role: Literal["user", "assistant"]
    text: str


class ConversationProvider(Protocol):
    """Produce one assistant response from ordered text history."""

    def respond(self, history: tuple[TextMessage, ...]) -> str:
        """Return one assistant text response for ``history``."""
