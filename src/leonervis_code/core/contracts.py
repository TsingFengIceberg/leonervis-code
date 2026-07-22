"""Structured contracts shared by the sequential model-tool loop."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import TYPE_CHECKING, Callable, Protocol, TypeAlias

if TYPE_CHECKING:
    from leonervis_code.core.compaction import EffectiveContextSummary

_SYSTEM_PROMPT_FINGERPRINT_DOMAIN = b"leonervis-code-system-prompt\0"


def system_prompt_fingerprint(version: int, text: str) -> str:
    """Return the stable domain-separated identity for exact prompt text."""
    if type(version) is not int or version < 1:
        raise ValueError("system prompt version must be positive")
    if not isinstance(text, str):
        raise ValueError("system prompt text must be text")
    encoded = (
        _SYSTEM_PROMPT_FINGERPRINT_DOMAIN
        + str(version).encode("ascii")
        + b"\0"
        + text.encode("utf-8")
    )
    return f"v{version}-{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class SystemPromptSnapshot:
    """One immutable, versioned system prompt sent with a provider request."""

    version: int
    text: str
    fingerprint: str


@dataclass(frozen=True)
class UserMessage:
    """One user text input in an ordered in-memory conversation."""

    text: str


@dataclass(frozen=True)
class AssistantText:
    """The final visible assistant text for one completed conversation turn."""

    text: str


@dataclass(frozen=True)
class ToolUse:
    """One provider-requested tool with one neutral string operand.

    ``path`` remains the schema-v1 compatibility field: it is a file path for
    ``read_file`` and a file-pattern operand for ``glob``.
    """

    tool_use_id: str
    name: str
    path: str


@dataclass(frozen=True)
class ToolResult:
    """One host-produced result corresponding to a ``ToolUse`` request."""

    tool_use_id: str
    content: str
    is_error: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ConversationTurn:
    """One completed user/final-assistant pair for REPL history display."""

    user: UserMessage
    assistant: AssistantText


@dataclass(frozen=True)
class CommittedTurn:
    """One complete causal turn ready for durable persistence and memory commit."""

    items: tuple[ConversationItem, ...]
    user: UserMessage
    assistant: AssistantText


ConversationItem: TypeAlias = UserMessage | AssistantText | ToolUse | ToolResult
TurnCommitter: TypeAlias = Callable[[CommittedTurn], None]
ProviderResponse: TypeAlias = AssistantText | ToolUse


@dataclass(frozen=True)
class ConversationRequest:
    """Provider-neutral model request with system policy separate from history."""

    system_prompt: SystemPromptSnapshot
    history: tuple[ConversationItem, ...]
    effective_summary: EffectiveContextSummary | None = None


class ConversationProvider(Protocol):
    """Produce one structured assistant response from a complete request snapshot."""

    def respond(self, request: ConversationRequest) -> ProviderResponse:
        """Return final assistant text or one requested tool action."""
