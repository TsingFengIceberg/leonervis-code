"""Structured contracts shared by the sequential model-tool loop."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Callable, Protocol, TypeAlias

if TYPE_CHECKING:
    from leonervis_code.core.compaction import EffectiveContextSummary

_SYSTEM_PROMPT_FINGERPRINT_DOMAIN = b"leonervis-code-system-prompt\0"
TOOL_ARGUMENTS_VERSION = 1
MAX_TOOL_ARGUMENTS_BYTES = 16 * 1024


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
class ToolArguments:
    """Immutable versioned provider-neutral arguments for one tool use."""

    version: int
    canonical_json: str

    def __post_init__(self) -> None:
        if self.version != TOOL_ARGUMENTS_VERSION:
            raise ValueError("unsupported tool arguments version")
        if not isinstance(self.canonical_json, str):
            raise ValueError("tool arguments canonical JSON must be text")
        try:
            decoded = json.loads(self.canonical_json)
        except json.JSONDecodeError:
            raise ValueError("tool arguments canonical JSON is invalid") from None
        if not isinstance(decoded, dict):
            raise ValueError("tool arguments must be a JSON object")
        canonical = self._canonicalize(decoded)
        if canonical != self.canonical_json:
            raise ValueError("tool arguments canonical JSON is not canonical")

    @classmethod
    def from_mapping(
        cls,
        arguments: dict[str, object],
        *,
        version: int = TOOL_ARGUMENTS_VERSION,
    ) -> ToolArguments:
        """Validate and freeze one JSON object in deterministic canonical form."""
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        return cls(version=version, canonical_json=cls._canonicalize(arguments))

    def as_mapping(self) -> dict[str, object]:
        """Return a fresh mutable projection of the frozen argument object."""
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise ValueError("tool arguments must decode to a JSON object")
        return value

    @staticmethod
    def _canonicalize(arguments: dict[str, object]) -> str:
        try:
            canonical = json.dumps(
                arguments,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            encoded = canonical.encode("utf-8")
        except (TypeError, ValueError, OverflowError, UnicodeEncodeError):
            raise ValueError("tool arguments are not canonical JSON") from None
        if len(encoded) > MAX_TOOL_ARGUMENTS_BYTES:
            raise ValueError(f"tool arguments exceed {MAX_TOOL_ARGUMENTS_BYTES} bytes")
        return canonical


@dataclass(frozen=True)
class ToolUse:
    """One provider-requested tool with immutable provider-neutral arguments."""

    tool_use_id: str
    name: str
    arguments: ToolArguments


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
