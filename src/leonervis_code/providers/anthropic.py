"""Explicit non-streaming Anthropic Messages adapter for Foundation 3A."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import anthropic

from leonervis_code.core.compaction import CompactSummaryRequest, EffectiveContextSummary
from leonervis_code.core.contracts import (
    AssistantText,
    ConversationItem,
    ConversationRequest,
    ProviderResponse,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.core.orchestration import ProviderFailureKind
from leonervis_code.providers.errors import (
    ProviderAdapterError,
    adapter_error,
    safe_request_id,
    safe_retry_after,
)
from leonervis_code.providers.model_context import (
    OFFICIAL_ANTHROPIC_BASE_URL,
    ModelContextDiscovery,
)
from leonervis_code.providers.request_context import (
    MAX_REQUEST_INPUT_TOKENS,
    RequestTokenCount,
    RequestTokenCountMethod,
    estimate_serialized_input_tokens,
)
from leonervis_code.tools.catalog import (
    model_tool_definitions,
    tool_input_from_use,
    tool_use_from_input,
)

PROVIDER_ID = "anthropic"
DEFAULT_MAX_OUTPUT_TOKENS = 1024


@dataclass(frozen=True)
class AnthropicProviderConfig:
    """Non-secret invocation settings for one explicit Anthropic adapter."""

    model_id: str
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    base_url: str = "https://api.anthropic.com"
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("Anthropic model ID must not be blank")
        if self.max_output_tokens < 1:
            raise ValueError("Anthropic max output tokens must be at least 1")
        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise ValueError("Anthropic temperature must be between 0.0 and 2.0")


class AnthropicModelsClient(Protocol):
    """The narrow synchronous Models API operation used for discovery."""

    def retrieve(self, model_id: str, **kwargs: object) -> object:
        """Retrieve metadata for one exact Anthropic model."""


class AnthropicMessagesClient(Protocol):
    """The narrow synchronous SDK operation used by the adapter."""

    def count_tokens(self, **kwargs: object) -> object:
        """Count input tokens for one Anthropic Messages projection."""

    def create(self, **kwargs: object) -> object:
        """Create one non-streaming Anthropic message."""


def create_anthropic_provider(
    config: AnthropicProviderConfig,
    *,
    api_key: str,
) -> AnthropicConversationProvider:
    """Construct the official synchronous SDK client at the credential boundary."""
    if not api_key.strip():
        raise _adapter_error(
            config,
            kind=ProviderFailureKind.AUTHENTICATION,
            code="missing_api_key",
            message="ANTHROPIC_API_KEY is not configured",
        )
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=config.base_url,
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(follow_redirects=False),
    )
    return AnthropicConversationProvider(
        config,
        client.messages,
        models_client=getattr(client, "models", None),
        owner=client,
    )


class AnthropicConversationProvider:
    """Serialize neutral causal history and decode one Anthropic response."""

    def __init__(
        self,
        config: AnthropicProviderConfig,
        client: AnthropicMessagesClient,
        *,
        models_client: AnthropicModelsClient | None = None,
        owner: object | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._models_client = models_client
        self._owner = owner

    def close(self) -> None:
        """Close the production SDK owner when this adapter constructed it."""
        close = getattr(self._owner, "close", None)
        if callable(close):
            close()

    def count_input_tokens(self, request_snapshot: ConversationRequest) -> RequestTokenCount:
        """Count official Anthropic input exactly, falling back to a safe estimate."""
        projection = build_input_projection(
            self._config,
            request_snapshot,
            committed_context=True,
        )
        if self._config.base_url.rstrip("/") != OFFICIAL_ANTHROPIC_BASE_URL:
            return estimate_serialized_input_tokens(projection)
        try:
            result = self._client.count_tokens(**projection)
            input_tokens = getattr(result, "input_tokens", None)
            if type(input_tokens) is not int or not (0 <= input_tokens <= MAX_REQUEST_INPUT_TOKENS):
                raise ValueError
            return RequestTokenCount(input_tokens, RequestTokenCountMethod.EXACT)
        except Exception:
            estimated = estimate_serialized_input_tokens(projection)
            return RequestTokenCount(
                estimated.input_tokens,
                RequestTokenCountMethod.ESTIMATED,
                "Anthropic token counting failed safely; used serialized estimate",
            )

    def count_compact_summary_input_tokens(
        self, request_snapshot: CompactSummaryRequest
    ) -> RequestTokenCount:
        """Count the exact no-tools compact-summary input projection."""
        projection = build_compact_summary_input_projection(self._config, request_snapshot)
        if self._config.base_url.rstrip("/") != OFFICIAL_ANTHROPIC_BASE_URL:
            return estimate_serialized_input_tokens(projection)
        try:
            result = self._client.count_tokens(**projection)
            input_tokens = getattr(result, "input_tokens", None)
            if type(input_tokens) is not int or not (0 <= input_tokens <= MAX_REQUEST_INPUT_TOKENS):
                raise ValueError
            return RequestTokenCount(input_tokens, RequestTokenCountMethod.EXACT)
        except Exception:
            estimated = estimate_serialized_input_tokens(projection)
            return RequestTokenCount(
                estimated.input_tokens,
                RequestTokenCountMethod.ESTIMATED,
                "Anthropic compact token counting failed safely; used serialized estimate",
            )

    def summarize_compact(self, request_snapshot: CompactSummaryRequest) -> AssistantText:
        """Generate one text-only summary without exposing workspace tools."""
        request = build_compact_summary_request(self._config, request_snapshot)
        try:
            response = self._client.create(**request)
        except anthropic.APIError as error:
            raise normalize_sdk_error(error, config=self._config) from None
        return parse_compact_summary_response(response, config=self._config)

    def respond(self, request_snapshot: ConversationRequest) -> ProviderResponse:
        """Make one non-streaming request through the injected SDK seam."""
        request = build_request(self._config, request_snapshot)
        try:
            response = self._client.create(**request)
        except anthropic.APIError as error:
            raise normalize_sdk_error(error, config=self._config) from None
        return parse_response(response, config=self._config)

    def discover_model_context(self) -> ModelContextDiscovery:
        """Discover one official Anthropic model's maximum input context."""
        if (
            self._models_client is None
            or self._config.base_url.rstrip("/") != OFFICIAL_ANTHROPIC_BASE_URL
        ):
            return ModelContextDiscovery(None, "live context discovery is unsupported")
        try:
            model = self._models_client.retrieve(self._config.model_id)
        except anthropic.APIError:
            return ModelContextDiscovery(None, "Anthropic model discovery failed safely")
        model_id = getattr(model, "id", None)
        max_input_tokens = getattr(model, "max_input_tokens", None)
        max_tokens = getattr(model, "max_tokens", None)
        if model_id != self._config.model_id:
            return ModelContextDiscovery(
                None, "Anthropic model discovery returned a different model ID"
            )
        context_value = (
            max_input_tokens if type(max_input_tokens) is int and max_input_tokens > 0 else None
        )
        output_value = max_tokens if type(max_tokens) is int and max_tokens > 0 else None
        diagnostic = None
        if context_value is None or output_value is None:
            diagnostic = "Anthropic model discovery returned an incomplete limit set"
        return ModelContextDiscovery(context_value, diagnostic, output_value)


def build_input_projection(
    config: AnthropicProviderConfig,
    request_snapshot: ConversationRequest,
    *,
    committed_context: bool = False,
) -> dict[str, object]:
    """Build the Anthropic fields that contribute provider input tokens."""
    return {
        "model": config.model_id,
        "system": request_snapshot.system_prompt.text,
        "messages": [
            *_serialize_effective_summary(request_snapshot.effective_summary),
            *serialize_history(
                request_snapshot.history,
                config=config,
                committed_context=committed_context,
            ),
        ],
        "tools": list(model_tool_definitions()),
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
    }


def build_request(
    config: AnthropicProviderConfig,
    request_snapshot: ConversationRequest,
) -> dict[str, object]:
    """Build one complete Anthropic Messages request deterministically."""
    request: dict[str, object] = {
        **build_input_projection(config, request_snapshot),
        "max_tokens": config.max_output_tokens,
        "stream": False,
    }
    if config.temperature is not None:
        request["temperature"] = config.temperature
    return request


def build_compact_summary_input_projection(
    config: AnthropicProviderConfig,
    request_snapshot: CompactSummaryRequest,
) -> dict[str, object]:
    """Build the no-tools Anthropic input projection for controlled summary."""
    return {
        "model": config.model_id,
        "system": request_snapshot.prompt.text,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": request_snapshot.source_text}],
            }
        ],
    }


def build_compact_summary_request(
    config: AnthropicProviderConfig,
    request_snapshot: CompactSummaryRequest,
) -> dict[str, object]:
    """Build one complete no-tools Anthropic summary request."""
    request: dict[str, object] = {
        **build_compact_summary_input_projection(config, request_snapshot),
        "max_tokens": request_snapshot.max_output_tokens,
        "stream": False,
    }
    if config.temperature is not None:
        request["temperature"] = config.temperature
    return request


def read_file_tool_definition() -> dict[str, object]:
    """Retain the tested Anthropic wrapper for the canonical read contract."""
    return model_tool_definitions()[0]


def glob_tool_definition() -> dict[str, object]:
    """Return the canonical Anthropic glob contract."""
    return model_tool_definitions()[1]


def grep_tool_definition() -> dict[str, object]:
    """Return the canonical Anthropic grep contract."""
    return model_tool_definitions()[2]


def write_file_tool_definition() -> dict[str, object]:
    """Return the canonical Anthropic controlled write contract."""
    return model_tool_definitions()[3]


def edit_file_tool_definition() -> dict[str, object]:
    """Return the canonical Anthropic controlled exact-edit contract."""
    return model_tool_definitions()[4]


def run_command_tool_definition() -> dict[str, object]:
    """Return the canonical Anthropic controlled command contract."""
    return model_tool_definitions()[5]


def mkdir_tool_definition() -> dict[str, object]:
    """Return the canonical Anthropic controlled directory contract."""
    return model_tool_definitions()[6]


def serialize_history(
    history: tuple[ConversationItem, ...],
    *,
    config: AnthropicProviderConfig,
    committed_context: bool = False,
) -> list[dict[str, object]]:
    """Convert neutral causal history for invocation or committed-context counting."""
    if not history:
        if committed_context:
            return []
        raise _invalid_history(config, "conversation history must not be empty")

    messages: list[dict[str, object]] = []
    expected = "user"
    pending_tool_use_id: str | None = None

    for item in history:
        if isinstance(item, UserMessage):
            if expected != "user" or not isinstance(item.text, str):
                raise _invalid_history(config, "user message is out of causal order")
            messages.append({"role": "user", "content": [{"type": "text", "text": item.text}]})
            expected = "assistant"
            continue

        if isinstance(item, AssistantText):
            if expected != "assistant" or not isinstance(item.text, str):
                raise _invalid_history(config, "assistant text is out of causal order")
            messages.append({"role": "assistant", "content": [{"type": "text", "text": item.text}]})
            expected = "user"
            continue

        if isinstance(item, ToolUse):
            if expected != "assistant":
                raise _invalid_history(config, "tool use is out of causal order")
            try:
                tool_input = tool_input_from_use(item)
            except ValueError:
                raise _invalid_history(
                    config, f"unsupported tool in history: {item.name}"
                ) from None
            if not isinstance(item.tool_use_id, str) or not item.tool_use_id:
                raise _invalid_history(config, "tool use ID must not be blank")
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": item.tool_use_id,
                            "name": item.name,
                            "input": tool_input,
                        }
                    ],
                }
            )
            pending_tool_use_id = item.tool_use_id
            expected = "tool_result"
            continue

        if isinstance(item, ToolResult):
            if expected != "tool_result" or item.tool_use_id != pending_tool_use_id:
                raise _invalid_history(config, "tool result does not match the pending tool use")
            if not isinstance(item.content, str):
                raise _invalid_history(config, "tool result content must be text")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": item.tool_use_id,
                            "content": item.content,
                            "is_error": item.is_error,
                        }
                    ],
                }
            )
            pending_tool_use_id = None
            expected = "assistant"
            continue

        raise _invalid_history(config, "conversation history contains an unknown item")

    valid_terminal_states = {"assistant"}
    if committed_context:
        valid_terminal_states.add("user")
    if expected not in valid_terminal_states:
        message = (
            "committed conversation history must end with assistant text"
            if committed_context
            else "conversation history must end before an assistant response"
        )
        raise _invalid_history(config, message)
    return messages


def _serialize_effective_summary(
    summary: EffectiveContextSummary | None,
) -> list[dict[str, object]]:
    if summary is None:
        return []
    return [
        {"role": "user", "content": [{"type": "text", "text": summary.user_text}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": summary.assistant_acknowledgement}],
        },
    ]


def parse_compact_summary_response(
    response: object,
    *,
    config: AnthropicProviderConfig,
) -> AssistantText:
    """Decode only a normally completed text-only compact summary."""
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        raise _adapter_error(
            config,
            kind=ProviderFailureKind.CONTENT_REFUSAL,
            code="content_refusal",
            message="Anthropic refused the compact summary request",
        )
    if stop_reason == "max_tokens":
        raise _invalid_response(config, "Anthropic compact summary reached the output-token limit")
    if stop_reason != "end_turn":
        raise _invalid_response(config, "Anthropic compact summary used an unsupported stop reason")
    content = getattr(response, "content", None)
    if not isinstance(content, list) or not content:
        raise _invalid_response(config, "Anthropic compact summary contained no content blocks")
    text_parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) != "text":
            raise _invalid_response(config, "Anthropic compact summary contained a non-text block")
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            raise _invalid_response(config, "Anthropic compact summary text was malformed")
        text_parts.append(text)
    text = "".join(text_parts).strip()
    if not text:
        raise _invalid_response(config, "Anthropic compact summary was empty")
    return AssistantText(text)


def parse_response(response: object, *, config: AnthropicProviderConfig) -> ProviderResponse:
    """Decode only complete text or one supported sequential tool response."""
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        raise _adapter_error(
            config,
            kind=ProviderFailureKind.CONTENT_REFUSAL,
            code="content_refusal",
            message="Anthropic refused the request",
        )
    if stop_reason == "max_tokens":
        raise _invalid_response(config, "Anthropic response reached the output-token limit")
    if stop_reason not in {"end_turn", "tool_use"}:
        raise _invalid_response(config, "Anthropic response used an unsupported stop reason")

    content = getattr(response, "content", None)
    if not isinstance(content, list) or not content:
        raise _invalid_response(config, "Anthropic response contained no content blocks")

    text_parts: list[str] = []
    tool_blocks: list[object] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                raise _invalid_response(config, "Anthropic text block was malformed")
            text_parts.append(text)
        elif block_type == "tool_use":
            tool_blocks.append(block)
        else:
            raise _invalid_response(config, "Anthropic response contained an unsupported block")

    if text_parts and tool_blocks:
        raise _invalid_response(config, "mixed text and tool-use responses are not supported")
    if text_parts:
        if stop_reason != "end_turn":
            raise _invalid_response(config, "text response did not end with end_turn")
        return AssistantText(text="".join(text_parts))
    if stop_reason != "tool_use":
        raise _invalid_response(config, "tool response did not end with tool_use")
    if len(tool_blocks) != 1:
        raise _invalid_response(config, "Anthropic response must contain exactly one tool use")

    block = tool_blocks[0]
    tool_use_id = getattr(block, "id", None)
    name = getattr(block, "name", None)
    tool_input = getattr(block, "input", None)
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise _invalid_response(config, "Anthropic tool use ID was malformed")
    if not isinstance(name, str):
        raise _invalid_response(config, "Anthropic requested an unsupported tool")
    if not isinstance(tool_input, dict):
        raise _invalid_response(config, f"Anthropic {name} input was malformed")
    try:
        return tool_use_from_input(tool_use_id, name, tool_input)
    except ValueError:
        raise _invalid_response(config, f"Anthropic {name} input was malformed") from None


def normalize_sdk_error(
    error: anthropic.APIError,
    *,
    config: AnthropicProviderConfig,
) -> ProviderAdapterError:
    """Map official SDK exceptions to stable failures without raw provider data."""
    if isinstance(error, anthropic.APIResponseValidationError):
        return _adapter_error(
            config,
            kind=ProviderFailureKind.RESPONSE_INVALID,
            code="sdk_response_invalid",
            message="Anthropic returned a response the SDK could not validate",
        )
    if isinstance(error, anthropic.APITimeoutError):
        return _adapter_error(
            config,
            kind=ProviderFailureKind.TIMEOUT,
            code="request_timeout",
            message="Anthropic request timed out",
            retryable=True,
        )
    if isinstance(error, anthropic.APIConnectionError):
        return _adapter_error(
            config,
            kind=ProviderFailureKind.TRANSPORT,
            code="connection_failed",
            message="could not connect to Anthropic",
            retryable=True,
        )

    status = getattr(error, "status_code", None)
    request_id = safe_request_id(getattr(error, "request_id", None))
    retry_after = safe_retry_after(getattr(getattr(error, "response", None), "headers", None))
    if isinstance(error, anthropic.AuthenticationError) or status == 401:
        kind = ProviderFailureKind.AUTHENTICATION
        code = "authentication_failed"
        message = "Anthropic rejected the API credential"
        retryable = False
    elif isinstance(error, anthropic.PermissionDeniedError) or status == 403:
        kind = ProviderFailureKind.AUTHORIZATION
        code = "permission_denied"
        message = "Anthropic denied access to the requested resource"
        retryable = False
    elif isinstance(error, anthropic.NotFoundError) or status == 404:
        kind = ProviderFailureKind.MODEL_UNAVAILABLE
        code = "model_unavailable"
        message = "the requested Anthropic model is unavailable"
        retryable = False
    elif isinstance(error, anthropic.RateLimitError) or status == 429:
        kind = ProviderFailureKind.RATE_LIMITED
        code = "rate_limited"
        message = "Anthropic rate-limited the request"
        retryable = True
    elif isinstance(error, anthropic.BadRequestError) or status in {400, 413, 422}:
        kind = ProviderFailureKind.INVALID_REQUEST
        code = "invalid_request"
        message = "Anthropic rejected the request as invalid"
        retryable = False
    elif isinstance(error, anthropic.InternalServerError) or (
        isinstance(status, int) and status >= 500
    ):
        kind = ProviderFailureKind.PROVIDER_UNAVAILABLE
        code = "provider_unavailable"
        message = "Anthropic is temporarily unavailable"
        retryable = True
    else:
        kind = ProviderFailureKind.TRANSPORT
        code = "sdk_failure"
        message = "the Anthropic SDK could not complete the request"
        retryable = False

    return _adapter_error(
        config,
        kind=kind,
        code=code,
        message=message,
        retryable=retryable,
        retry_after_seconds=retry_after,
        request_id=request_id,
    )


def _invalid_history(config: AnthropicProviderConfig, message: str) -> ProviderAdapterError:
    return _adapter_error(
        config,
        kind=ProviderFailureKind.INVALID_REQUEST,
        code="invalid_history",
        message=message,
    )


def _invalid_response(config: AnthropicProviderConfig, message: str) -> ProviderAdapterError:
    return _adapter_error(
        config,
        kind=ProviderFailureKind.RESPONSE_INVALID,
        code="response_invalid",
        message=message,
    )


def _adapter_error(
    config: AnthropicProviderConfig,
    *,
    kind: ProviderFailureKind,
    code: str,
    message: str,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
    request_id: str | None = None,
) -> ProviderAdapterError:
    return adapter_error(
        provider_id=PROVIDER_ID,
        model_id=config.model_id,
        kind=kind,
        code=code,
        message=message,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
        request_id=request_id,
    )
