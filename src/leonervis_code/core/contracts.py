"""Core contracts shared by the first Harness learning slices."""

from __future__ import annotations

from typing import Protocol


class PromptProvider(Protocol):
    """Produce one text response for one prompt."""

    def respond(self, prompt: str) -> str:
        """Return the provider response for ``prompt``."""
