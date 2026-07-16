"""The bounded orchestration loop for Foundation 1B."""

from __future__ import annotations

from leonervis_code.core.contracts import (
    AssistantText,
    ConversationItem,
    ConversationProvider,
    ConversationTurn,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.tools.read_file import ReadFileTool

MAX_TOOL_CALLS = 3


class ToolLoopLimitError(RuntimeError):
    """Raised when a provider does not finish after its tool-call budget is exhausted."""


class AgentLoop:
    """Maintain atomic in-memory turns across a bounded provider/tool loop."""

    def __init__(
        self,
        provider: ConversationProvider | None,
        read_file: ReadFileTool,
    ) -> None:
        """Store an optional default provider, confined tool, and empty conversation state."""
        self._provider = provider
        self._read_file = read_file
        self._history: tuple[ConversationItem, ...] = ()
        self._turns: tuple[ConversationTurn, ...] = ()

    @property
    def history(self) -> tuple[ConversationItem, ...]:
        """Return the complete ordered causal context of completed turns."""
        return self._history

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        """Return completed user/final-assistant pairs for user-facing history display."""
        return self._turns

    def run(
        self,
        prompt: str,
        *,
        provider: ConversationProvider | None = None,
    ) -> str:
        """Run one bounded tool loop with one provider pinned for the full turn."""
        turn_provider = provider or self._provider
        if turn_provider is None:
            raise RuntimeError("conversation provider is required for this turn")
        user = UserMessage(text=prompt)
        candidate: tuple[ConversationItem, ...] = self._history + (user,)
        tool_calls = 0

        while True:
            response = turn_provider.respond(candidate)
            if isinstance(response, AssistantText):
                self._history = candidate + (response,)
                self._turns += (ConversationTurn(user=user, assistant=response),)
                return response.text

            candidate += (response,)
            if tool_calls == MAX_TOOL_CALLS:
                candidate += (
                    ToolResult(
                        tool_use_id=response.tool_use_id,
                        content="tool call limit reached for this conversation turn",
                        is_error=True,
                    ),
                )
                final_response = turn_provider.respond(candidate)
                if isinstance(final_response, AssistantText):
                    self._history = candidate + (final_response,)
                    self._turns += (ConversationTurn(user=user, assistant=final_response),)
                    return final_response.text
                raise ToolLoopLimitError("provider requested a tool after the tool call limit")

            tool_calls += 1
            candidate += (self._execute(response),)

    def _execute(self, request: ToolUse) -> ToolResult:
        """Dispatch the only Foundation 1B tool or return a model-visible error."""
        if request.name == "read_file":
            return self._read_file.execute(request)
        return ToolResult(
            tool_use_id=request.tool_use_id,
            content=f"unknown tool: {request.name}",
            is_error=True,
        )
