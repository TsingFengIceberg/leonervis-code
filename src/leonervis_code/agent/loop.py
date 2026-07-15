"""The bounded orchestration loop for Foundation 1A."""

from __future__ import annotations

from leonervis_code.core.contracts import ConversationProvider, TextMessage


class AgentLoop:
    """Maintain one in-memory ordered text conversation."""

    def __init__(self, provider: ConversationProvider) -> None:
        """Store the provider and begin with no completed conversation turns."""
        self._provider = provider
        self._history: tuple[TextMessage, ...] = ()

    @property
    def history(self) -> tuple[TextMessage, ...]:
        """Return the completed user/assistant message pairs in order."""
        return self._history

    def run(self, prompt: str) -> str:
        """Append one completed turn after exactly one successful provider call."""
        user_message = TextMessage(role="user", text=prompt)
        requested_history = self._history + (user_message,)
        response = self._provider.respond(requested_history)
        self._history = requested_history + (TextMessage(role="assistant", text=response),)
        return response
