from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.core.compaction import (
    CompactSummaryRequest,
    EffectiveContextSummary,
    build_compact_prompt,
)
from leonervis_code.core.contracts import (
    AssistantText,
    ConversationRequest,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.core.orchestration import ProviderFailureKind
from leonervis_code.providers.anthropic import (
    AnthropicConversationProvider,
    AnthropicProviderConfig,
    create_anthropic_provider,
    normalize_sdk_error,
    parse_compact_summary_response,
    parse_response,
    read_file_tool_definition,
    serialize_history,
)
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.request_context import RequestTokenCountMethod
from leonervis_code.system_prompt import build_system_prompt
from leonervis_code.tools.read_file import ReadFileTool


class RecordingModelsClient:
    def __init__(self, outcomes: list[object | Exception]) -> None:
        self.outcomes = outcomes
        self.model_ids: list[str] = []

    def retrieve(self, model_id: str, **kwargs: object) -> object:
        self.model_ids.append(model_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RecordingMessagesClient:
    def __init__(
        self,
        outcomes: list[object | Exception],
        *,
        counts: list[object | Exception] | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.counts = counts or []
        self.requests: list[dict[str, object]] = []
        self.count_requests: list[dict[str, object]] = []

    def count_tokens(self, **kwargs: object) -> object:
        self.count_requests.append(kwargs)
        outcome = self.counts.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def message(
    *blocks: TextBlock | ToolUseBlock,
    stop_reason: str | None = None,
) -> Message:
    resolved_stop_reason = stop_reason
    if resolved_stop_reason is None:
        resolved_stop_reason = (
            "tool_use" if any(block.type == "tool_use" for block in blocks) else "end_turn"
        )
    return Message(
        id="msg_test",
        content=list(blocks),
        model="claude-opus-4-8",
        role="assistant",
        stop_reason=resolved_stop_reason,
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def config() -> AnthropicProviderConfig:
    return AnthropicProviderConfig(model_id="claude-opus-4-8", max_output_tokens=64)


def request(*history) -> ConversationRequest:
    return ConversationRequest(system_prompt=build_system_prompt(), history=tuple(history))


def test_official_token_count_uses_shared_input_projection_and_safe_fallback() -> None:
    client = RecordingMessagesClient(
        [],
        counts=[SimpleNamespace(input_tokens=321), RuntimeError("secret raw count failure")],
    )
    provider = AnthropicConversationProvider(config(), client)
    snapshot = request(UserMessage("hello"))

    exact = provider.count_input_tokens(snapshot)
    estimated = provider.count_input_tokens(snapshot)

    assert exact.input_tokens == 321
    assert exact.method == RequestTokenCountMethod.EXACT
    assert estimated.method == RequestTokenCountMethod.ESTIMATED
    assert "secret" not in (estimated.diagnostic or "")
    assert set(client.count_requests[0]) == {"model", "system", "messages", "tools"}


def test_counter_accepts_empty_and_complete_committed_history_without_weakening_send() -> None:
    client = RecordingMessagesClient(
        [message(TextBlock(text="unused", type="text"))],
        counts=[SimpleNamespace(input_tokens=7), SimpleNamespace(input_tokens=9)],
    )
    provider = AnthropicConversationProvider(config(), client)

    empty = provider.count_input_tokens(request())
    complete = provider.count_input_tokens(request(UserMessage("hello"), AssistantText("reply")))

    assert empty.input_tokens == 7
    assert client.count_requests[0]["messages"] == []
    assert complete.input_tokens == 9
    assert client.count_requests[1]["messages"][-1]["role"] == "assistant"
    with pytest.raises(ProviderAdapterError, match="before an assistant response"):
        provider.respond(request(UserMessage("hello"), AssistantText("reply")))
    assert client.requests == []


def test_production_client_uses_explicit_route_and_disables_redirects(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.messages = RecordingMessagesClient([])
            self.models = RecordingModelsClient([])

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient-untrusted.example")
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)

    provider = create_anthropic_provider(
        AnthropicProviderConfig(
            model_id="claude-opus-4-8",
            base_url="https://route-owned.example",
        ),
        api_key="secret",
    )

    assert isinstance(provider, AnthropicConversationProvider)
    assert captured["base_url"] == "https://route-owned.example"
    assert captured["max_retries"] == 0
    assert captured["http_client"].follow_redirects is False
    captured["http_client"].close()


def test_serializer_preserves_every_current_causal_item_and_tool_id() -> None:
    history = (
        UserMessage(text="Read the file"),
        ToolUse(tool_use_id="toolu_1", name="read_file", path="README.md"),
        ToolResult(tool_use_id="toolu_1", content="notes\n", is_error=False),
        AssistantText(text="Done"),
        UserMessage(text="Continue"),
    )

    assert serialize_history(history, config=config()) == [
        {"role": "user", "content": [{"type": "text", "text": "Read the file"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "notes\n",
                    "is_error": False,
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        {"role": "user", "content": [{"type": "text", "text": "Continue"}]},
    ]


def test_serializer_rejects_unknown_tools_and_broken_causality() -> None:
    with pytest.raises(ProviderAdapterError) as unknown:
        serialize_history(
            (
                UserMessage(text="Search"),
                ToolUse(tool_use_id="toolu_1", name="search", path="README.md"),
                ToolResult(tool_use_id="toolu_1", content="result"),
            ),
            config=config(),
        )
    assert unknown.value.failure.kind == ProviderFailureKind.INVALID_REQUEST

    with pytest.raises(ProviderAdapterError, match="does not match"):
        serialize_history(
            (
                UserMessage(text="Read"),
                ToolUse(tool_use_id="toolu_1", name="read_file", path="README.md"),
                ToolResult(tool_use_id="other", content="result"),
            ),
            config=config(),
        )


def test_read_file_schema_is_exact_and_closed() -> None:
    assert read_file_tool_definition() == {
        "name": "read_file",
        "description": (
            "Read one workspace-relative UTF-8 text file when its contents are needed to "
            "answer the user. This tool is read-only and its bounded output may be truncated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to one UTF-8 text file in the workspace.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }


def test_parser_concatenates_text_and_preserves_valid_tool_use() -> None:
    assert parse_response(
        message(TextBlock(text="one", type="text"), TextBlock(text=" two", type="text")),
        config=config(),
    ) == AssistantText(text="one two")
    assert parse_response(
        message(
            ToolUseBlock(
                id="toolu_provider",
                name="read_file",
                input={"path": "README.md"},
                type="tool_use",
            )
        ),
        config=config(),
    ) == ToolUse(tool_use_id="toolu_provider", name="read_file", path="README.md")


@pytest.mark.parametrize(
    "response",
    [
        message(),
        message(
            TextBlock(text="preface", type="text"),
            ToolUseBlock(
                id="toolu_1", name="read_file", input={"path": "README.md"}, type="tool_use"
            ),
        ),
        message(ToolUseBlock(id="toolu_1", name="search", input={"path": "x"}, type="tool_use")),
        message(ToolUseBlock(id="toolu_1", name="read_file", input={}, type="tool_use")),
        message(ToolUseBlock(id="toolu_1", name="read_file", input={"path": 1}, type="tool_use")),
        message(
            ToolUseBlock(id="toolu_1", name="read_file", input={"path": "a"}, type="tool_use"),
            ToolUseBlock(id="toolu_2", name="read_file", input={"path": "b"}, type="tool_use"),
        ),
    ],
)
def test_parser_rejects_response_shapes_the_loop_cannot_represent(response: Message) -> None:
    with pytest.raises(ProviderAdapterError) as caught:
        parse_response(response, config=config())
    assert caught.value.failure.kind == ProviderFailureKind.RESPONSE_INVALID


def test_parser_classifies_refusal_and_rejects_truncated_text() -> None:
    refused = message(TextBlock(text="I cannot help", type="text"), stop_reason="refusal")
    with pytest.raises(ProviderAdapterError) as refusal:
        parse_response(refused, config=config())
    assert refusal.value.failure.kind == ProviderFailureKind.CONTENT_REFUSAL

    truncated = message(TextBlock(text="partial", type="text"), stop_reason="max_tokens")
    with pytest.raises(ProviderAdapterError) as output_limit:
        parse_response(truncated, config=config())
    assert output_limit.value.failure.kind == ProviderFailureKind.RESPONSE_INVALID


def test_adapter_sends_explicit_temperature_when_configured() -> None:
    client = RecordingMessagesClient([message(TextBlock(text="Hello", type="text"))])
    configured = AnthropicProviderConfig(
        model_id="claude-opus-4-8",
        max_output_tokens=64,
        temperature=0.2,
    )
    provider = AnthropicConversationProvider(configured, client)

    provider.respond(request(UserMessage(text="Hello")))

    assert client.requests[0]["temperature"] == 0.2


def test_adapter_sends_only_explicit_native_request_fields() -> None:
    client = RecordingMessagesClient([message(TextBlock(text="Hello", type="text"))])
    provider = AnthropicConversationProvider(config(), client)

    assert provider.respond(request(UserMessage(text="Hello"))) == AssistantText(text="Hello")
    assert client.requests == [
        {
            "model": "claude-opus-4-8",
            "max_tokens": 64,
            "system": build_system_prompt().text,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            "tools": [read_file_tool_definition()],
            "stream": False,
        }
    ]


def compact_request() -> CompactSummaryRequest:
    return CompactSummaryRequest(build_compact_prompt(), '{"turns":[]}', 32)


def test_compact_summary_count_and_create_omit_tools_and_parse_text_only() -> None:
    client = RecordingMessagesClient(
        [message(TextBlock(text=" summary ", type="text"))],
        counts=[SimpleNamespace(input_tokens=12)],
    )
    provider = AnthropicConversationProvider(config(), client)

    counted = provider.count_compact_summary_input_tokens(compact_request())
    result = provider.summarize_compact(compact_request())

    assert counted.input_tokens == 12
    assert set(client.count_requests[0]) == {"model", "system", "messages"}
    assert result == AssistantText("summary")
    assert "tools" not in client.requests[0]
    assert client.requests[0]["max_tokens"] == 32


def test_compact_summary_parser_rejects_tools_refusal_and_truncation() -> None:
    with pytest.raises(ProviderAdapterError):
        parse_compact_summary_response(
            message(
                ToolUseBlock(
                    id="toolu_1",
                    name="read_file",
                    input={"path": "README.md"},
                    type="tool_use",
                )
            ),
            config=config(),
        )
    for stop_reason in ("refusal", "max_tokens"):
        with pytest.raises(ProviderAdapterError):
            parse_compact_summary_response(
                message(TextBlock(text="partial", type="text"), stop_reason=stop_reason),
                config=config(),
            )


def test_effective_summary_is_projected_before_retained_history() -> None:
    summary = EffectiveContextSummary("old state")
    snapshot = ConversationRequest(
        build_system_prompt(),
        (UserMessage("recent"),),
        effective_summary=summary,
    )
    client = RecordingMessagesClient([message(TextBlock(text="done", type="text"))])

    AnthropicConversationProvider(config(), client).respond(snapshot)

    messages = client.requests[0]["messages"]
    assert messages[0]["content"][0]["text"] == summary.user_text
    assert messages[1]["content"][0]["text"] == summary.assistant_acknowledgement
    assert messages[2]["content"][0]["text"] == "recent"


def test_anthropic_models_discovery_is_exact_and_safe() -> None:
    models = RecordingModelsClient(
        [
            SimpleNamespace(
                id="claude-opus-4-8",
                max_input_tokens=1_000_000,
                max_tokens=128_000,
            )
        ]
    )
    provider = AnthropicConversationProvider(
        config(), RecordingMessagesClient([]), models_client=models
    )

    discovered = provider.discover_model_context()

    assert discovered.context_window_tokens == 1_000_000
    assert discovered.model_max_output_tokens == 128_000
    assert discovered.diagnostic is None
    assert models.model_ids == ["claude-opus-4-8"]

    mismatched = AnthropicConversationProvider(
        config(),
        RecordingMessagesClient([]),
        models_client=RecordingModelsClient(
            [SimpleNamespace(id="other", max_input_tokens=1_000_000)]
        ),
    ).discover_model_context()
    assert mismatched.context_window_tokens is None
    assert "different model ID" in mismatched.diagnostic

    missing = AnthropicConversationProvider(
        config(),
        RecordingMessagesClient([]),
        models_client=RecordingModelsClient(
            [SimpleNamespace(id="claude-opus-4-8", max_input_tokens=None)]
        ),
    ).discover_model_context()
    assert missing.context_window_tokens is None
    assert "incomplete limit set" in missing.diagnostic


@dataclass
class ErrorCase:
    error: anthropic.APIError
    kind: ProviderFailureKind
    retryable: bool


def status_error(
    error_type: type[anthropic.APIStatusError], status: int, *, retry_after: str | None = None
) -> anthropic.APIStatusError:
    headers = {"request-id": "req_safe"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    response = httpx.Response(
        status,
        headers=headers,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return error_type("raw provider body sk-ant-secret", response=response, body={"secret": "x"})


@pytest.mark.parametrize(
    "case",
    [
        ErrorCase(
            status_error(anthropic.AuthenticationError, 401),
            ProviderFailureKind.AUTHENTICATION,
            False,
        ),
        ErrorCase(
            status_error(anthropic.PermissionDeniedError, 403),
            ProviderFailureKind.AUTHORIZATION,
            False,
        ),
        ErrorCase(
            status_error(anthropic.BadRequestError, 400), ProviderFailureKind.INVALID_REQUEST, False
        ),
        ErrorCase(
            status_error(anthropic.NotFoundError, 404), ProviderFailureKind.MODEL_UNAVAILABLE, False
        ),
        ErrorCase(
            status_error(anthropic.RateLimitError, 429, retry_after="3"),
            ProviderFailureKind.RATE_LIMITED,
            True,
        ),
        ErrorCase(
            status_error(anthropic.InternalServerError, 503),
            ProviderFailureKind.PROVIDER_UNAVAILABLE,
            True,
        ),
        ErrorCase(
            anthropic.APIResponseValidationError(
                httpx.Response(
                    200,
                    request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
                ),
                {"secret": "sk-ant-secret"},
                message="raw invalid response",
            ),
            ProviderFailureKind.RESPONSE_INVALID,
            False,
        ),
        ErrorCase(
            anthropic.APITimeoutError(httpx.Request("POST", "https://api.anthropic.com")),
            ProviderFailureKind.TIMEOUT,
            True,
        ),
        ErrorCase(
            anthropic.APIConnectionError(
                message="raw secret sk-ant-secret",
                request=httpx.Request("POST", "https://api.anthropic.com"),
            ),
            ProviderFailureKind.TRANSPORT,
            True,
        ),
    ],
)
def test_sdk_errors_are_safely_normalized(case: ErrorCase) -> None:
    normalized = normalize_sdk_error(case.error, config=config())

    assert normalized.failure.kind == case.kind
    assert normalized.failure.retryable is case.retryable
    assert "sk-ant-secret" not in normalized.failure.message
    assert "raw provider body" not in normalized.failure.message
    if case.kind == ProviderFailureKind.RATE_LIMITED:
        assert normalized.failure.retry_after_seconds == 3
        assert normalized.failure.request_id == "req_safe"


def test_adapter_backed_loop_preserves_atomic_commit_after_failure(tmp_path) -> None:
    (tmp_path / "README.md").write_text("workspace notes\n", encoding="utf-8")
    failure = status_error(anthropic.InternalServerError, 503)
    client = RecordingMessagesClient(
        [
            message(
                ToolUseBlock(
                    id="toolu_read",
                    name="read_file",
                    input={"path": "README.md"},
                    type="tool_use",
                )
            ),
            failure,
        ]
    )
    loop = AgentLoop(AnthropicConversationProvider(config(), client), ReadFileTool(tmp_path))

    with pytest.raises(ProviderAdapterError):
        loop.run("Read README")

    assert loop.history == ()
    assert loop.turns == ()
    assert client.requests[1]["messages"][-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_read",
                "content": "workspace notes\n",
                "is_error": False,
            }
        ],
    }
