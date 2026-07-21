"""The bounded orchestration loop for Foundation 1B."""

from __future__ import annotations

from collections.abc import Callable

from leonervis_code.core.contracts import (
    AssistantText,
    CommittedTurn,
    ConversationItem,
    ConversationProvider,
    ConversationRequest,
    ConversationTurn,
    SystemPromptSnapshot,
    ToolResult,
    ToolUse,
    TurnCommitter,
    UserMessage,
)
from leonervis_code.core.effective_context import (
    EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
    EffectiveContextSnapshot,
    validate_complete_history,
)
from leonervis_code.system_prompt import build_system_prompt
from leonervis_code.tools.read_file import (
    MAX_READ_FILE_EXECUTIONS_PER_TURN,
    READ_FILE_TOOL_NAME,
    ReadFileTool,
    read_file_tool_snapshot,
)

SystemPromptFactory = Callable[[], SystemPromptSnapshot]


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
        system_prompt_factory: SystemPromptFactory = build_system_prompt,
    ) -> None:
        """Store a provider, confined tool, validated history, and durable commit hook."""
        self._provider = provider
        self._read_file = read_file
        restored = validate_complete_history(initial_history)
        self._full_history = restored.history
        self._effective_history = restored.history
        self._turns = restored.display_turns
        self._commit_turn = commit_turn
        self._system_prompt_factory = system_prompt_factory

    @property
    def history(self) -> tuple[ConversationItem, ...]:
        """Return the complete ordered causal context of completed turns."""
        return self._full_history

    @property
    def effective_history(self) -> tuple[ConversationItem, ...]:
        """Return the committed causal context currently visible to providers."""
        return self._effective_history

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        """Return completed user/final-assistant pairs for user-facing history display."""
        return self._turns

    def effective_context_snapshot(self) -> EffectiveContextSnapshot:
        """Freeze the full and provider-visible committed context without mutation."""
        return EffectiveContextSnapshot(
            representation_version=EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
            source=EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
            system_prompt=self._system_prompt_factory(),
            tool_definitions=(read_file_tool_snapshot(),),
            full_history=self._full_history,
            effective_history=self._effective_history,
        )

    def committed_context_request(self) -> ConversationRequest:
        """Retain the committed-count compatibility seam through effective context."""
        return self.effective_context_snapshot().to_conversation_request()

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
        context = self.effective_context_snapshot()
        pending: tuple[ConversationItem, ...] = (user,)
        tool_calls = 0

        while True:
            response = turn_provider.respond(context.to_conversation_request(pending_items=pending))
            if isinstance(response, AssistantText):
                self._commit(pending + (response,), user, response)
                return response.text

            pending += (response,)
            if tool_calls == MAX_READ_FILE_EXECUTIONS_PER_TURN:
                pending += (
                    ToolResult(
                        tool_use_id=response.tool_use_id,
                        content="tool call limit reached for this conversation turn",
                        is_error=True,
                    ),
                )
                final_response = turn_provider.respond(
                    context.to_conversation_request(pending_items=pending)
                )
                if isinstance(final_response, AssistantText):
                    self._commit(pending + (final_response,), user, final_response)
                    return final_response.text
                raise ToolLoopLimitError("provider requested a tool after the tool call limit")

            tool_calls += 1
            pending += (self._execute(response),)

    def _commit(
        self,
        items: tuple[ConversationItem, ...],
        user: UserMessage,
        assistant: AssistantText,
    ) -> None:
        """Persist one complete turn before exposing it through in-memory state."""
        turn = CommittedTurn(
            items=items,
            user=user,
            assistant=assistant,
        )
        if self._commit_turn is not None:
            self._commit_turn(turn)
        self._full_history += items
        self._effective_history += items
        self._turns += (ConversationTurn(user=user, assistant=assistant),)

    def _execute(self, request: ToolUse) -> ToolResult:
        """Dispatch the only Foundation 1B tool or return a model-visible error."""
        if request.name == READ_FILE_TOOL_NAME:
            return self._read_file.execute(request)
        return ToolResult(
            tool_use_id=request.tool_use_id,
            content=f"unknown tool: {request.name}",
            is_error=True,
        )


def restore_history(
    history: tuple[ConversationItem, ...],
) -> tuple[tuple[ConversationItem, ...], tuple[ConversationTurn, ...]]:
    """Retain the public restoration seam through the canonical validator."""
    validated = validate_complete_history(history)
    return validated.history, validated.display_turns
