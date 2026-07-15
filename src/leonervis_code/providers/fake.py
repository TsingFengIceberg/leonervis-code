"""Deterministic provider implementations for learning and tests."""

from __future__ import annotations

from collections.abc import Sequence

from leonervis_code.core.contracts import TextMessage


class ScriptedFakeProvider:
    """Record ordered histories and return deterministic scripted responses."""

    def __init__(self, script: Sequence[str | Exception] | None = None) -> None:
        """Create a default echo fake or consume the supplied response script."""
        self._script = tuple(script) if script is not None else None
        self._next_outcome = 0
        self._received_histories: list[tuple[TextMessage, ...]] = []

    @property
    def received_histories(self) -> tuple[tuple[TextMessage, ...], ...]:
        """Return immutable snapshots of every provider request."""
        return tuple(self._received_histories)

    def respond(self, history: tuple[TextMessage, ...]) -> str:
        """Record ``history`` and return its next deterministic outcome."""
        self._received_histories.append(tuple(history))
        if self._script is None:
            return f"Fake response: {history[-1].text}"
        if self._next_outcome == len(self._script):
            raise RuntimeError("fake provider script is exhausted")

        outcome = self._script[self._next_outcome]
        self._next_outcome += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
