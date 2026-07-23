"""The bounded orchestration loop for the current sequential tool surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from leonervis_code.core.actions import ActionLease
from leonervis_code.core.compaction import EffectiveContextSummary
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
    COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT,
    EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
    EffectiveContextSnapshot,
    validate_complete_history,
)
from leonervis_code.system_prompt import build_system_prompt
from leonervis_code.tools.catalog import MAX_TOOL_EXECUTIONS_PER_TURN, TOOL_CATALOG
from leonervis_code.tools.glob import GLOB_TOOL_NAME, GlobTool
from leonervis_code.tools.grep import GREP_TOOL_NAME, GrepTool
from leonervis_code.tools.read_file import READ_FILE_TOOL_NAME, ReadFileTool

SystemPromptFactory = Callable[[], SystemPromptSnapshot]
ActionDispatcher = Callable[[ToolUse, ActionLease], ToolResult]


class ToolLoopLimitError(RuntimeError):
    """Raised when a provider does not finish after its tool-call budget is exhausted."""


@dataclass(frozen=True)
class PreparedAgentTurn:
    """One pending user item pinned to one committed Effective Context."""

    user: UserMessage
    context: EffectiveContextSnapshot
    pending_items: tuple[ConversationItem, ...]
    action_lease: ActionLease | None = None

    def __post_init__(self) -> None:
        if self.pending_items != (self.user,):
            raise ValueError("prepared turn must contain exactly its pending user message")

    @property
    def initial_request(self) -> ConversationRequest:
        return self.context.to_conversation_request(pending_items=self.pending_items)

    def rebase(self, context: EffectiveContextSnapshot) -> PreparedAgentTurn:
        if self.action_lease is not None:
            raise ValueError("a leased prepared turn cannot be rebased")
        return replace(self, context=context)

    def with_action_lease(self, lease: ActionLease) -> PreparedAgentTurn:
        """Bind one non-recreatable lease after automatic compaction is complete."""
        if self.action_lease is not None:
            raise ValueError("prepared turn already has an action lease")
        if lease.context_id != self.context.context_id:
            raise ValueError("action lease context does not match prepared turn")
        return replace(self, action_lease=lease)


class AgentLoop:
    """Maintain atomic in-memory turns across a bounded provider/tool loop."""

    def __init__(
        self,
        provider: ConversationProvider | None,
        read_file: ReadFileTool,
        glob: GlobTool,
        grep: GrepTool,
        *,
        initial_history: tuple[ConversationItem, ...] = (),
        initial_effective_history: tuple[ConversationItem, ...] | None = None,
        initial_effective_summary: EffectiveContextSummary | None = None,
        initial_effective_source: str = EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
        commit_turn: TurnCommitter | None = None,
        system_prompt_factory: SystemPromptFactory = build_system_prompt,
        action_dispatcher: ActionDispatcher | None = None,
    ) -> None:
        """Store a provider, confined tool, validated history, and durable commit hook."""
        self._provider = provider
        self._read_file = read_file
        self._glob = glob
        self._grep = grep
        restored = validate_complete_history(initial_history)
        effective_items = (
            restored.history if initial_effective_history is None else initial_effective_history
        )
        validate_complete_history(effective_items)
        if initial_effective_source == EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY:
            if initial_effective_summary is not None or effective_items != restored.history:
                raise ValueError("full-history effective context must equal full history")
        elif initial_effective_source == EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT:
            if initial_effective_summary is None:
                raise ValueError("compacted effective context requires a summary")
            effective_turns = validate_complete_history(effective_items).complete_turns
            full_turns = restored.complete_turns
            if len(effective_turns) > len(full_turns) or (
                effective_turns and full_turns[-len(effective_turns) :] != effective_turns
            ):
                raise ValueError("compacted effective history must be a full-history turn suffix")
        else:
            raise ValueError("unsupported effective-context source")
        self._full_history = restored.history
        self._effective_history = effective_items
        self._effective_summary = initial_effective_summary
        self._effective_source = initial_effective_source
        self._turns = restored.display_turns
        self._commit_turn = commit_turn
        self._system_prompt_factory = system_prompt_factory
        self._action_dispatcher = action_dispatcher

    @property
    def history(self) -> tuple[ConversationItem, ...]:
        """Return the complete ordered causal context of completed turns."""
        return self._full_history

    @property
    def effective_history(self) -> tuple[ConversationItem, ...]:
        """Return the committed causal context currently visible to providers."""
        return self._effective_history

    @property
    def effective_summary(self) -> EffectiveContextSummary | None:
        """Return the Host-produced prefix currently visible to providers."""
        return self._effective_summary

    @property
    def effective_source(self) -> str:
        """Return the durable source kind for current effective context."""
        return self._effective_source

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        """Return completed user/final-assistant pairs for user-facing history display."""
        return self._turns

    def effective_context_snapshot(self) -> EffectiveContextSnapshot:
        """Freeze the full and provider-visible committed context without mutation."""
        representation_version = (
            EFFECTIVE_CONTEXT_REPRESENTATION_VERSION
            if self._effective_summary is None
            else COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION
        )
        return EffectiveContextSnapshot(
            representation_version=representation_version,
            source=self._effective_source,
            system_prompt=self._system_prompt_factory(),
            tool_definitions=TOOL_CATALOG,
            full_history=self._full_history,
            effective_history=self._effective_history,
            effective_summary=self._effective_summary,
        )

    def committed_context_request(self) -> ConversationRequest:
        """Retain the committed-count compatibility seam through effective context."""
        return self.effective_context_snapshot().to_conversation_request()

    def prepare_turn(self, prompt: str) -> PreparedAgentTurn:
        """Freeze one pending user message without mutating conversation state."""
        user = UserMessage(text=prompt)
        return PreparedAgentTurn(
            user=user,
            context=self.effective_context_snapshot(),
            pending_items=(user,),
        )

    def run(
        self,
        prompt: str,
        *,
        provider: ConversationProvider | None = None,
    ) -> str:
        """Prepare then run one bounded tool loop for compatibility callers."""
        return self.run_prepared(self.prepare_turn(prompt), provider=provider)

    def run_prepared(
        self,
        prepared: PreparedAgentTurn,
        *,
        provider: ConversationProvider | None = None,
    ) -> str:
        """Run one prebuilt pending turn against its pinned committed context."""
        turn_provider = provider or self._provider
        if turn_provider is None:
            raise RuntimeError("conversation provider is required for this turn")
        user = prepared.user
        context = prepared.context
        pending = prepared.pending_items
        tool_calls = 0

        while True:
            response = turn_provider.respond(context.to_conversation_request(pending_items=pending))
            if isinstance(response, AssistantText):
                self._commit(pending + (response,), user, response)
                return response.text

            pending += (response,)
            if tool_calls == MAX_TOOL_EXECUTIONS_PER_TURN:
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
            pending += (self._execute(response, prepared.action_lease),)

    def install_action_dispatcher(self, dispatcher: ActionDispatcher) -> None:
        """Install the ProjectSession-owned permission/audit dispatch seam exactly once."""
        if self._action_dispatcher is not None:
            raise ValueError("action dispatcher is already installed")
        self._action_dispatcher = dispatcher

    def install_compaction(
        self,
        *,
        summary: EffectiveContextSummary,
        retained_history: tuple[ConversationItem, ...],
    ) -> None:
        """Install a prevalidated durable checkpoint with non-fallible assignments."""
        self._effective_summary = summary
        self._effective_history = retained_history
        self._effective_source = EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT

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
        full_validated = validate_complete_history(self._full_history)
        validate_complete_history(
            items,
            prior_tool_use_ids=full_validated.tool_use_ids,
        )
        if self._commit_turn is not None:
            self._commit_turn(turn)
        self._full_history += items
        self._effective_history += items
        self._turns += (ConversationTurn(user=user, assistant=assistant),)

    def _execute(self, request: ToolUse, lease: ActionLease | None) -> ToolResult:
        """Dispatch one current tool through the Host action boundary when installed."""
        if self._action_dispatcher is not None:
            if lease is None:
                raise RuntimeError("prepared action lease is required")
            return self._action_dispatcher(request, lease)
        if request.name == READ_FILE_TOOL_NAME:
            return self._read_file.execute(request)
        if request.name == GLOB_TOOL_NAME:
            return self._glob.execute(request)
        if request.name == GREP_TOOL_NAME:
            return self._grep.execute(request)
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
