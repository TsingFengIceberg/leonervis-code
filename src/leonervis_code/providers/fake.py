"""Deterministic provider implementations for learning and tests."""

from __future__ import annotations


class DeterministicFakeProvider:
    """Return a stable local response without contacting a model service."""

    def respond(self, prompt: str) -> str:
        """Return a reproducible response that preserves the supplied prompt."""
        return f"Fake response: {prompt}"
