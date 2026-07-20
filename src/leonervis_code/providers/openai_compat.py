"""Non-streaming OpenAI-compatible chat-completions adapter for Foundation 3B."""

from __future__ import annotations

import json
from typing import Protocol

import openai

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
from leonervis_code.providers.definitions import RuntimeProviderRoute
from leonervis_code.providers.errors import (
    ProviderAdapterError,
    adapter_error,
    safe_request_id,
    safe_retry_after,
)
from leonervis_code.tools.read_file import read_file_model_definition


class ChatCompletionsClient(Protocol):
    """The narrow synchronous SDK operation used by the compatible adapter."""

    def create(self, **kwargs: object) -> object:
        """Create one non-streaming chat completion."""


class OpenAICompatibleConversationProvider:
    """Translate neutral causal history through an OpenAI-compatible endpoint."""

    def __init__(
        self,
        route: RuntimeProviderRoute,
        client: ChatCompletionsClient,
        *,
        owner: object | None = None,
    ) -> None:
        self._route = route
        self._client = client
        self._owner = owner

    def close(self) -> None:
        """Close the production SDK owner when this adapter constructed it."""
        close = getattr(self._owner, "close", None)
        if callable(close):
            close()

    def respond(self, request_snapshot: ConversationRequest) -> ProviderResponse:
        """Make one non-streaming compatible request through the injected seam."""
        request = build_request(self._route, request_snapshot)
        try:
            response = self._client.create(**request)
        except openai.APIError as error:
            raise normalize_sdk_error(error, route=self._route) from None
        return parse_response(response, route=self._route)


def create_openai_compatible_provider(
    route: RuntimeProviderRoute,
    *,
    api_key: str | None,
) -> OpenAICompatibleConversationProvider:
    """Construct the official OpenAI SDK at the selected credential boundary."""
    definition = route.definition
    if definition.credential_required and not (api_key and api_key.strip()):
        raise adapter_error(
            provider_id=definition.provider_id,
            model_id=route.selected_model,
            kind=ProviderFailureKind.AUTHENTICATION,
            code="missing_api_key",
            message=f"{definition.credential_env} is not configured",
        )
    client = openai.OpenAI(
        api_key=api_key or "local-no-auth",
        base_url=route.base_url,
        max_retries=0,
        http_client=openai.DefaultHttpxClient(follow_redirects=False),
    )
    return OpenAICompatibleConversationProvider(route, client.chat.completions, owner=client)


def read_file_tool_definition() -> dict[str, object]:
    """Wrap the shared read_file contract as one compatible function tool."""
    definition = read_file_model_definition()
    return {
        "type": "function",
        "function": {
            "name": definition["name"],
            "description": definition["description"],
            "parameters": definition["input_schema"],
        },
    }


def build_request(
    route: RuntimeProviderRoute,
    request_snapshot: ConversationRequest,
) -> dict[str, object]:
    """Build a complete provider-native request with deterministic compatibility rules."""
    messages = [
        {"role": "system", "content": request_snapshot.system_prompt.text},
        *serialize_history(request_snapshot.history, route=route),
    ]
    request: dict[str, object] = {
        "model": route.wire_model,
        "messages": messages,
        "tools": [read_file_tool_definition()],
        "parallel_tool_calls": False,
        "stream": False,
    }
    token_field = token_limit_field(route.wire_model)
    request[token_field] = route.max_output_tokens
    if route.temperature is not None and not fixed_sampling_model(route.wire_model):
        request["temperature"] = route.temperature
    _validate_request_size(route, request)
    return request


def serialize_history(
    history: tuple[ConversationItem, ...],
    *,
    route: RuntimeProviderRoute,
) -> list[dict[str, object]]:
    """Translate the current neutral causal sequence to chat-completions messages."""
    if not history:
        raise _invalid_history(route, "conversation history must not be empty")
    messages: list[dict[str, object]] = []
    expected = "user"
    pending_tool_use_id: str | None = None

    for item in history:
        if isinstance(item, UserMessage):
            if expected != "user":
                raise _invalid_history(route, "user message is out of causal order")
            messages.append({"role": "user", "content": item.text})
            expected = "assistant"
            continue
        if isinstance(item, AssistantText):
            if expected != "assistant":
                raise _invalid_history(route, "assistant text is out of causal order")
            messages.append({"role": "assistant", "content": item.text})
            expected = "user"
            continue
        if isinstance(item, ToolUse):
            if expected != "assistant":
                raise _invalid_history(route, "tool use is out of causal order")
            if item.name != "read_file":
                raise _invalid_history(route, f"unsupported tool in history: {item.name}")
            if not item.tool_use_id:
                raise _invalid_history(route, "tool use ID must not be blank")
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.tool_use_id,
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps(
                                    {"path": item.path}, separators=(",", ":"), ensure_ascii=False
                                ),
                            },
                        }
                    ],
                }
            )
            pending_tool_use_id = item.tool_use_id
            expected = "tool_result"
            continue
        if isinstance(item, ToolResult):
            if expected != "tool_result" or item.tool_use_id != pending_tool_use_id:
                raise _invalid_history(route, "tool result does not match the pending tool use")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.tool_use_id,
                    "content": item.content,
                }
            )
            pending_tool_use_id = None
            expected = "assistant"
            continue
        raise _invalid_history(route, "conversation history contains an unknown item")

    if expected != "assistant":
        raise _invalid_history(route, "conversation history must end before an assistant response")
    return messages


def parse_response(response: object, *, route: RuntimeProviderRoute) -> ProviderResponse:
    """Decode only complete text or exactly one valid read_file function call."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or len(choices) != 1:
        raise _invalid_response(route, "provider response must contain exactly one choice")
    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason in {"length", "max_tokens"}:
        raise _invalid_response(route, "provider response reached the output-token limit")
    if finish_reason in {"content_filter", "refusal"}:
        raise adapter_error(
            provider_id=route.definition.provider_id,
            model_id=route.selected_model,
            kind=ProviderFailureKind.CONTENT_REFUSAL,
            code="content_refusal",
            message="provider refused or filtered the request",
        )
    if finish_reason not in {"stop", "tool_calls"}:
        raise _invalid_response(route, "provider response used an unsupported finish reason")

    message = getattr(choice, "message", None)
    if message is None:
        raise _invalid_response(route, "provider response choice contained no message")
    content = getattr(message, "content", None)
    refusal = getattr(message, "refusal", None)
    if refusal:
        raise adapter_error(
            provider_id=route.definition.provider_id,
            model_id=route.selected_model,
            kind=ProviderFailureKind.CONTENT_REFUSAL,
            code="content_refusal",
            message="provider refused the request",
        )
    tool_calls = getattr(message, "tool_calls", None) or []
    if content not in {None, ""} and tool_calls:
        raise _invalid_response(route, "mixed text and tool-call responses are not supported")
    if finish_reason == "stop":
        if not isinstance(content, str) or not content:
            raise _invalid_response(route, "provider text response was empty or malformed")
        if tool_calls:
            raise _invalid_response(route, "text response unexpectedly contained tool calls")
        return AssistantText(text=content)
    if content not in {None, ""} or len(tool_calls) != 1:
        raise _invalid_response(route, "tool response must contain exactly one tool call")

    call = tool_calls[0]
    tool_use_id = getattr(call, "id", None)
    function = getattr(call, "function", None)
    name = getattr(function, "name", None)
    arguments = getattr(function, "arguments", None)
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise _invalid_response(route, "provider tool call ID was malformed")
    if name != "read_file":
        raise _invalid_response(route, "provider requested an unsupported tool")
    if not isinstance(arguments, str):
        raise _invalid_response(route, "provider tool arguments were not JSON text")
    try:
        tool_input = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        raise _invalid_response(route, "provider tool arguments were invalid JSON") from None
    if not isinstance(tool_input, dict) or set(tool_input) != {"path"}:
        raise _invalid_response(route, "provider read_file arguments were malformed")
    path = tool_input["path"]
    if not isinstance(path, str):
        raise _invalid_response(route, "provider read_file path was not text")
    return ToolUse(tool_use_id=tool_use_id, name=name, path=path)


def token_limit_field(model: str) -> str:
    """Select the documented token-limit field for compatible reasoning families."""
    base = model.rsplit("/", 1)[-1].lower()
    return "max_completion_tokens" if base.startswith("gpt-5") else "max_tokens"


def fixed_sampling_model(model: str) -> bool:
    """Return whether known reasoning families reject tuning controls."""
    base = model.rsplit("/", 1)[-1].lower()
    return base.startswith(("o1", "o3", "o4", "gpt-5")) or "thinking" in base


def normalize_sdk_error(
    error: openai.APIError,
    *,
    route: RuntimeProviderRoute,
) -> ProviderAdapterError:
    """Map official SDK failures without retaining raw response or credential data."""
    provider_id = route.definition.provider_id
    model_id = route.selected_model
    if isinstance(error, openai.APITimeoutError):
        return adapter_error(
            provider_id=provider_id,
            model_id=model_id,
            kind=ProviderFailureKind.TIMEOUT,
            code="request_timeout",
            message=f"{provider_id} request timed out",
            retryable=True,
        )
    if isinstance(error, openai.APIConnectionError):
        return adapter_error(
            provider_id=provider_id,
            model_id=model_id,
            kind=ProviderFailureKind.TRANSPORT,
            code="connection_failed",
            message=f"could not connect to {provider_id}",
            retryable=True,
        )
    status = getattr(error, "status_code", None)
    request_id = safe_request_id(getattr(error, "request_id", None))
    retry_after = safe_retry_after(getattr(getattr(error, "response", None), "headers", None))
    if isinstance(error, openai.AuthenticationError) or status == 401:
        kind, code, message, retryable = (
            ProviderFailureKind.AUTHENTICATION,
            "authentication_failed",
            f"{provider_id} rejected the API credential",
            False,
        )
    elif isinstance(error, openai.PermissionDeniedError) or status == 403:
        kind, code, message, retryable = (
            ProviderFailureKind.AUTHORIZATION,
            "permission_denied",
            f"{provider_id} denied access to the requested resource",
            False,
        )
    elif isinstance(error, openai.NotFoundError) or status == 404:
        kind, code, message, retryable = (
            ProviderFailureKind.MODEL_UNAVAILABLE,
            "model_unavailable",
            f"the requested {provider_id} model is unavailable",
            False,
        )
    elif isinstance(error, openai.RateLimitError) or status == 429:
        kind, code, message, retryable = (
            ProviderFailureKind.RATE_LIMITED,
            "rate_limited",
            f"{provider_id} rate-limited the request",
            True,
        )
    elif isinstance(error, openai.BadRequestError) or status in {400, 413, 422}:
        kind, code, message, retryable = (
            ProviderFailureKind.INVALID_REQUEST,
            "invalid_request",
            f"{provider_id} rejected the request as invalid",
            False,
        )
    elif isinstance(error, openai.InternalServerError) or (
        isinstance(status, int) and status >= 500
    ):
        kind, code, message, retryable = (
            ProviderFailureKind.PROVIDER_UNAVAILABLE,
            "provider_unavailable",
            f"{provider_id} is temporarily unavailable",
            True,
        )
    else:
        kind, code, message, retryable = (
            ProviderFailureKind.TRANSPORT,
            "sdk_failure",
            f"the {provider_id} SDK could not complete the request",
            False,
        )
    return adapter_error(
        provider_id=provider_id,
        model_id=model_id,
        kind=kind,
        code=code,
        message=message,
        retryable=retryable,
        retry_after_seconds=retry_after,
        request_id=request_id,
    )


def _validate_request_size(route: RuntimeProviderRoute, request: dict[str, object]) -> None:
    encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > route.definition.request_body_limit:
        raise adapter_error(
            provider_id=route.definition.provider_id,
            model_id=route.selected_model,
            kind=ProviderFailureKind.INVALID_REQUEST,
            code="request_body_too_large",
            message="provider request exceeds the configured body-size limit",
        )


def _invalid_history(route: RuntimeProviderRoute, message: str) -> ProviderAdapterError:
    return adapter_error(
        provider_id=route.definition.provider_id,
        model_id=route.selected_model,
        kind=ProviderFailureKind.INVALID_REQUEST,
        code="invalid_history",
        message=message,
    )


def _invalid_response(route: RuntimeProviderRoute, message: str) -> ProviderAdapterError:
    return adapter_error(
        provider_id=route.definition.provider_id,
        model_id=route.selected_model,
        kind=ProviderFailureKind.RESPONSE_INVALID,
        code="response_invalid",
        message=message,
    )
