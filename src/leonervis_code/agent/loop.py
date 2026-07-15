"""The bounded orchestration loop for Foundation 0."""

from __future__ import annotations

from leonervis_code.core.contracts import PromptProvider


class AgentLoop:
    """Delegate one prompt to one provider without retaining state."""

    def __init__(self, provider: PromptProvider) -> None:
        """Store the provider used for this one-turn loop."""
        self._provider = provider

    def run(self, prompt: str) -> str:
        """Return the result of exactly one provider call."""
        return self._provider.respond(prompt)
