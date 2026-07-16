"""Deterministic provider implementations for learning and tests."""

from __future__ import annotations

from collections.abc import Sequence

from leonervis_code.core.contracts import (
    AssistantText,
    ConversationItem,
    ProviderResponse,
    UserMessage,
)


class ScriptedFakeProvider:
    """Record structured contexts and return deterministic scripted responses."""

    def __init__(self, script: Sequence[ProviderResponse | Exception] | None = None) -> None:
        """Create a default echo fake or consume the supplied response script."""
        self._script = tuple(script) if script is not None else None
        self._next_outcome = 0
        self._received_histories: list[tuple[ConversationItem, ...]] = []

    @property
    def received_histories(self) -> tuple[tuple[ConversationItem, ...], ...]:
        """Return immutable snapshots of every provider request."""
        return tuple(self._received_histories)

    def respond(self, history: tuple[ConversationItem, ...]) -> ProviderResponse:
        """Record ``history`` and return its next deterministic outcome."""
        self._received_histories.append(tuple(history))
        if self._script is None:
            latest_user = next(item for item in reversed(history) if isinstance(item, UserMessage))
            return AssistantText(text=f"Fake response: {latest_user.text}")
        if self._next_outcome == len(self._script):
            raise RuntimeError("fake provider script is exhausted")

        outcome = self._script[self._next_outcome]
        self._next_outcome += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
