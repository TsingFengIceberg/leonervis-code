"""Provider-neutral effective-context state and stable content identity."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from leonervis_code.core.compaction import EffectiveContextSummary
from leonervis_code.core.contracts import (
    AssistantText,
    ConversationItem,
    ConversationRequest,
    ConversationTurn,
    SystemPromptSnapshot,
    ToolArguments,
    ToolResult,
    ToolUse,
    UserMessage,
    system_prompt_fingerprint,
)

EFFECTIVE_CONTEXT_REPRESENTATION_VERSION = 1
COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION = 2
EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY = "full_committed_history"
EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT = "compact_checkpoint"
_EFFECTIVE_CONTEXT_ID_DOMAIN = b"leonervis-code-effective-context-id\0"


@dataclass(frozen=True)
class CompleteConversationTurn:
    """One complete causal turn, including every atomic tool pair."""

    items: tuple[ConversationItem, ...]
    user: UserMessage
    assistant: AssistantText


@dataclass(frozen=True)
class ValidatedConversationHistory:
    """One strictly partitioned complete committed conversation history."""

    history: tuple[ConversationItem, ...]
    complete_turns: tuple[CompleteConversationTurn, ...]
    display_turns: tuple[ConversationTurn, ...]
    tool_use_ids: frozenset[str]


@dataclass(frozen=True)
class CanonicalToolDefinition:
    """One immutable provider-neutral tool definition encoded canonically."""

    name: str
    canonical_json: str

    @classmethod
    def from_mapping(cls, definition: dict[str, object]) -> CanonicalToolDefinition:
        if not isinstance(definition, dict):
            raise ValueError("tool definition must be a JSON object")
        name = definition.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool definition name must not be blank")
        canonical = _canonical_json(definition, label="tool definition")
        decoded = json.loads(canonical)
        if not isinstance(decoded, dict) or decoded.get("name") != name:
            raise ValueError("tool definition canonical form is invalid")
        return cls(name=name, canonical_json=canonical)

    def as_mapping(self) -> dict[str, object]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise ValueError("canonical tool definition must decode to an object")
        return value


@dataclass(frozen=True)
class EffectiveContextSnapshot:
    """Full transcript truth plus the committed context visible to providers."""

    representation_version: int
    source: str
    system_prompt: SystemPromptSnapshot
    tool_definitions: tuple[CanonicalToolDefinition, ...]
    full_history: tuple[ConversationItem, ...]
    effective_history: tuple[ConversationItem, ...]
    effective_summary: EffectiveContextSummary | None = None

    def __post_init__(self) -> None:
        supported = {
            EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
            COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
        }
        if self.representation_version not in supported:
            raise ValueError("unsupported effective-context representation version")
        if self.source not in {
            EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
            EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT,
        }:
            raise ValueError("unsupported effective-context source")
        _validate_system_prompt_snapshot(self.system_prompt)
        if not isinstance(self.tool_definitions, tuple) or not self.tool_definitions:
            raise ValueError("effective context requires immutable tool definitions")
        tool_names: set[str] = set()
        for definition in self.tool_definitions:
            if not isinstance(definition, CanonicalToolDefinition):
                raise ValueError("effective context contains an invalid tool definition")
            CanonicalToolDefinition.from_mapping(definition.as_mapping())
            if definition.name in tool_names:
                raise ValueError("effective context contains a duplicate tool definition")
            tool_names.add(definition.name)
        validate_complete_history(self.full_history)
        validate_complete_history(self.effective_history)
        if self.source == EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY:
            if self.representation_version != EFFECTIVE_CONTEXT_REPRESENTATION_VERSION:
                raise ValueError("full-history effective context must use representation version 1")
            if self.effective_summary is not None:
                raise ValueError("full-history effective context must not contain a summary")
            if self.full_history != self.effective_history:
                raise ValueError("full-history effective context must equal full history")
        else:
            if self.representation_version != COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION:
                raise ValueError("compacted effective context must use representation version 2")
            if not isinstance(self.effective_summary, EffectiveContextSummary):
                raise ValueError("compacted effective context requires a summary")
            if not _is_complete_turn_suffix(self.full_turns, self.effective_turns):
                raise ValueError("compacted effective history must be a full-history turn suffix")

    @property
    def full_turns(self) -> tuple[CompleteConversationTurn, ...]:
        return validate_complete_history(self.full_history).complete_turns

    @property
    def effective_turns(self) -> tuple[CompleteConversationTurn, ...]:
        return validate_complete_history(self.effective_history).complete_turns

    @property
    def full_turn_count(self) -> int:
        return len(self.full_turns)

    @property
    def full_item_count(self) -> int:
        return len(self.full_history)

    @property
    def effective_turn_count(self) -> int:
        return len(self.effective_turns)

    @property
    def effective_item_count(self) -> int:
        return len(self.effective_history)

    @property
    def context_id(self) -> str:
        manifest = {
            "representation_version": self.representation_version,
            "system_prompt": {
                "version": self.system_prompt.version,
                "text": self.system_prompt.text,
                "fingerprint": self.system_prompt.fingerprint,
            },
            "tool_definitions": [definition.as_mapping() for definition in self.tool_definitions],
            "effective_turns": [
                {"items": [_item_identity(item) for item in turn.items]}
                for turn in self.effective_turns
            ],
        }
        if self.effective_summary is not None:
            manifest["effective_summary"] = {
                "assistant_acknowledgement": self.effective_summary.assistant_acknowledgement,
                "continuation_fingerprint": self.effective_summary.continuation_fingerprint,
                "continuation_version": self.effective_summary.continuation_version,
                "user_text": self.effective_summary.user_text,
            }
        payload = _canonical_json(manifest, label="effective context").encode("utf-8")
        digest = hashlib.sha256(_EFFECTIVE_CONTEXT_ID_DOMAIN + payload).hexdigest()
        return f"ctx-v{self.representation_version}-{digest}"

    def to_conversation_request(
        self,
        *,
        pending_items: tuple[ConversationItem, ...] = (),
    ) -> ConversationRequest:
        """Project effective history plus one optional uncommitted turn suffix."""
        if not isinstance(pending_items, tuple):
            raise ValueError("pending conversation items must be a tuple")
        return ConversationRequest(
            system_prompt=self.system_prompt,
            history=self.effective_history + pending_items,
            effective_summary=self.effective_summary,
        )


def _is_complete_turn_suffix(
    full_turns: tuple[CompleteConversationTurn, ...],
    effective_turns: tuple[CompleteConversationTurn, ...],
) -> bool:
    if len(effective_turns) > len(full_turns):
        return False
    if not effective_turns:
        return True
    return full_turns[-len(effective_turns) :] == effective_turns


def validate_complete_history(
    history: tuple[ConversationItem, ...],
    *,
    prior_tool_use_ids: frozenset[str] = frozenset(),
) -> ValidatedConversationHistory:
    """Validate and partition sequential complete turns without splitting tool pairs."""
    if not isinstance(history, tuple):
        raise ValueError("conversation history must be a tuple")
    if not isinstance(prior_tool_use_ids, frozenset) or not all(
        isinstance(value, str) and value for value in prior_tool_use_ids
    ):
        raise ValueError("prior tool use IDs must be non-empty text")

    complete_turns: list[CompleteConversationTurn] = []
    display_turns: list[ConversationTurn] = []
    seen_tool_ids = set(prior_tool_use_ids)
    index = 0
    while index < len(history):
        start = index
        user = history[index]
        _validate_item(user)
        if not isinstance(user, UserMessage):
            raise ValueError("conversation turn must start with a user message")
        index += 1

        while index < len(history) and isinstance(history[index], ToolUse):
            request = history[index]
            _validate_item(request)
            if request.tool_use_id in seen_tool_ids:
                raise ValueError(f"duplicate tool use ID: {request.tool_use_id}")
            if index + 1 >= len(history):
                raise ValueError("conversation history has an unmatched tool use")
            result = history[index + 1]
            _validate_item(result)
            if not isinstance(result, ToolResult) or result.tool_use_id != request.tool_use_id:
                raise ValueError("conversation tool result does not match its tool use")
            seen_tool_ids.add(request.tool_use_id)
            index += 2

        if index >= len(history):
            raise ValueError("conversation turn must end with assistant text")
        assistant = history[index]
        _validate_item(assistant)
        if not isinstance(assistant, AssistantText):
            raise ValueError("conversation turn must end with assistant text")
        index += 1
        items = history[start:index]
        complete_turns.append(CompleteConversationTurn(items, user, assistant))
        display_turns.append(ConversationTurn(user, assistant))

    return ValidatedConversationHistory(
        history=history,
        complete_turns=tuple(complete_turns),
        display_turns=tuple(display_turns),
        tool_use_ids=frozenset(seen_tool_ids),
    )


def _validate_system_prompt_snapshot(snapshot: SystemPromptSnapshot) -> None:
    if not isinstance(snapshot, SystemPromptSnapshot):
        raise ValueError("system prompt snapshot is invalid")
    expected = system_prompt_fingerprint(snapshot.version, snapshot.text)
    if snapshot.fingerprint != expected:
        raise ValueError("system prompt fingerprint does not match its version and text")


def _validate_item(item: object) -> None:
    if isinstance(item, (UserMessage, AssistantText)):
        if not isinstance(item.text, str):
            raise ValueError("conversation text must be text")
        return
    if isinstance(item, ToolUse):
        if not isinstance(item.tool_use_id, str) or not item.tool_use_id:
            raise ValueError("tool use ID must not be blank")
        if not isinstance(item.name, str) or not item.name:
            raise ValueError("tool use name must not be blank")
        if not isinstance(item.arguments, ToolArguments):
            raise ValueError("tool use arguments are invalid")
        try:
            item.arguments.as_mapping()
        except (AttributeError, ValueError):
            raise ValueError("tool use arguments are invalid") from None
        return
    if isinstance(item, ToolResult):
        if not isinstance(item.tool_use_id, str) or not item.tool_use_id:
            raise ValueError("tool result ID must not be blank")
        if not isinstance(item.content, str):
            raise ValueError("tool result content must be text")
        if type(item.is_error) is not bool or type(item.truncated) is not bool:
            raise ValueError("tool result flags must be booleans")
        return
    raise ValueError("conversation history contains an unknown item")


def _item_identity(item: ConversationItem) -> dict[str, object]:
    _validate_item(item)
    if isinstance(item, UserMessage):
        return {"item_type": "user_message", "text": item.text}
    if isinstance(item, AssistantText):
        return {"item_type": "assistant_text", "text": item.text}
    if isinstance(item, ToolUse):
        return {
            "item_type": "tool_use",
            "tool_use_id": item.tool_use_id,
            "name": item.name,
            "arguments_version": item.arguments.version,
            "arguments": item.arguments.as_mapping(),
        }
    assert isinstance(item, ToolResult)
    return {
        "item_type": "tool_result",
        "tool_use_id": item.tool_use_id,
        "content": item.content,
        "is_error": item.is_error,
        "truncated": item.truncated,
    }


def _canonical_json(value: object, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} is not canonical JSON") from error
