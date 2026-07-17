"""The bounded orchestration loop for Foundation 1B."""

from __future__ import annotations

from leonervis_code.core.contracts import (
    AssistantText,
    CommittedTurn,
    ConversationItem,
    ConversationProvider,
    ConversationTurn,
    ToolResult,
    ToolUse,
    TurnCommitter,
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
        *,
        initial_history: tuple[ConversationItem, ...] = (),
        commit_turn: TurnCommitter | None = None,
    ) -> None:
        """Store a provider, confined tool, validated history, and durable commit hook."""
        self._provider = provider
        self._read_file = read_file
        self._history, self._turns = restore_history(initial_history)
        self._commit_turn = commit_turn

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
                self._commit(candidate + (response,), user, response)
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
                    self._commit(candidate + (final_response,), user, final_response)
                    return final_response.text
                raise ToolLoopLimitError("provider requested a tool after the tool call limit")

            tool_calls += 1
            candidate += (self._execute(response),)

    def _commit(
        self,
        history: tuple[ConversationItem, ...],
        user: UserMessage,
        assistant: AssistantText,
    ) -> None:
        """Persist one complete turn before exposing it through in-memory state."""
        turn = CommittedTurn(
            items=history[len(self._history) :],
            user=user,
            assistant=assistant,
        )
        if self._commit_turn is not None:
            self._commit_turn(turn)
        self._history = history
        self._turns += (ConversationTurn(user=user, assistant=assistant),)

    def _execute(self, request: ToolUse) -> ToolResult:
        """Dispatch the only Foundation 1B tool or return a model-visible error."""
        if request.name == "read_file":
            return self._read_file.execute(request)
        return ToolResult(
            tool_use_id=request.tool_use_id,
            content=f"unknown tool: {request.name}",
            is_error=True,
        )


def restore_history(
    history: tuple[ConversationItem, ...],
) -> tuple[tuple[ConversationItem, ...], tuple[ConversationTurn, ...]]:
    """Validate complete causal turns and derive the user-facing turn view."""
    if not isinstance(history, tuple):
        raise ValueError("conversation history must be a tuple")
    turns: list[ConversationTurn] = []
    index = 0
    seen_tool_ids: set[str] = set()
    while index < len(history):
        user = history[index]
        if not isinstance(user, UserMessage):
            raise ValueError("conversation turn must start with a user message")
        index += 1
        while index < len(history) and isinstance(history[index], ToolUse):
            request = history[index]
            assert isinstance(request, ToolUse)
            if request.tool_use_id in seen_tool_ids:
                raise ValueError(f"duplicate tool use ID: {request.tool_use_id}")
            if index + 1 >= len(history):
                raise ValueError("conversation history has an unmatched tool use")
            result = history[index + 1]
            if not isinstance(result, ToolResult) or result.tool_use_id != request.tool_use_id:
                raise ValueError("conversation tool result does not match its tool use")
            seen_tool_ids.add(request.tool_use_id)
            index += 2
        if index >= len(history) or not isinstance(history[index], AssistantText):
            raise ValueError("conversation turn must end with assistant text")
        assistant = history[index]
        assert isinstance(assistant, AssistantText)
        turns.append(ConversationTurn(user=user, assistant=assistant))
        index += 1
    return tuple(history), tuple(turns)
