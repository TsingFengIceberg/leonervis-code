from __future__ import annotations

from dataclasses import replace

import openai
import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.core.contracts import (
    AssistantText,
    ConversationRequest,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.core.orchestration import ProviderFailureKind
from leonervis_code.providers.definitions import OPENAI
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.openai_compat import (
    OpenAICompatibleConversationProvider,
    build_request,
    create_openai_compatible_provider,
    parse_response,
    read_file_tool_definition,
    serialize_history,
)
from leonervis_code.providers.request_context import RequestTokenCountMethod
from leonervis_code.providers.resolver import resolve_runtime_route
from leonervis_code.system_prompt import build_system_prompt
from leonervis_code.tools.read_file import ReadFileTool


class RecordingChatClient:
    def __init__(self, outcomes: list[object | Exception]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_compatible_counter_estimates_the_shared_native_input_projection() -> None:
    provider = OpenAICompatibleConversationProvider(route(), RecordingChatClient([]))
    counted = provider.count_input_tokens(request(UserMessage("hello")))

    assert counted.method == RequestTokenCountMethod.ESTIMATED
    assert counted.input_tokens is not None and counted.input_tokens > 0


def route(selector: str = "openai/gpt-4.1"):
    return resolve_runtime_route(selector, environment={})


def request(*history) -> ConversationRequest:
    return ConversationRequest(system_prompt=build_system_prompt(), history=tuple(history))


def completion(
    *,
    content: str | None = None,
    finish_reason: str = "stop",
    tool_calls: list[ChatCompletionMessageFunctionToolCall] | None = None,
    refusal: str | None = None,
) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl_test",
        choices=[
            Choice(
                finish_reason=finish_reason,
                index=0,
                logprobs=None,
                message=ChatCompletionMessage(
                    role="assistant",
                    content=content,
                    refusal=refusal,
                    tool_calls=tool_calls,
                ),
            )
        ],
        created=0,
        model="test-model",
        object="chat.completion",
    )


def tool_call(
    *,
    call_id: str = "call_1",
    name: str = "read_file",
    arguments: str = '{"path":"README.md"}',
) -> ChatCompletionMessageFunctionToolCall:
    return ChatCompletionMessageFunctionToolCall(
        id=call_id,
        type="function",
        function=Function(name=name, arguments=arguments),
    )


def test_production_client_uses_route_base_url_and_disables_redirects(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.chat = type("Chat", (), {"completions": RecordingChatClient([])})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    provider = create_openai_compatible_provider(route(), api_key="secret")

    assert isinstance(provider, OpenAICompatibleConversationProvider)
    assert captured["base_url"] == "https://api.openai.com/v1"
    assert captured["max_retries"] == 0
    assert captured["http_client"].follow_redirects is False
    captured["http_client"].close()


def test_serializer_preserves_tool_call_and_result_pairing() -> None:
    history = (
        UserMessage(text="Read"),
        ToolUse(tool_use_id="call_provider", name="read_file", path="README.md"),
        ToolResult(tool_use_id="call_provider", content="notes\n", is_error=False),
    )

    assert serialize_history(history, route=route()) == [
        {"role": "user", "content": "Read"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_provider",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_provider", "content": "notes\n"},
    ]


def test_tool_schema_is_exact_and_closed() -> None:
    assert read_file_tool_definition() == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read one workspace-relative UTF-8 text file when its contents are needed to "
                "answer the user. This tool is read-only and its bounded output may be truncated."
            ),
            "parameters": {
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
        },
    }


def test_parser_decodes_complete_text_and_one_read_file_call() -> None:
    assert parse_response(completion(content="Hello"), route=route()) == AssistantText(text="Hello")
    assert parse_response(
        completion(finish_reason="tool_calls", tool_calls=[tool_call(call_id="call_provider")]),
        route=route(),
    ) == ToolUse(tool_use_id="call_provider", name="read_file", path="README.md")


@pytest.mark.parametrize(
    "response",
    [
        ChatCompletion(id="x", choices=[], created=0, model="m", object="chat.completion"),
        completion(content="partial", finish_reason="length"),
        completion(content="preface", finish_reason="tool_calls", tool_calls=[tool_call()]),
        completion(
            finish_reason="tool_calls", tool_calls=[tool_call(), tool_call(call_id="call_2")]
        ),
        completion(finish_reason="tool_calls", tool_calls=[tool_call(arguments="not json")]),
        completion(finish_reason="tool_calls", tool_calls=[tool_call(arguments='{"path":1}')]),
        completion(finish_reason="tool_calls", tool_calls=[tool_call(name="search")]),
    ],
)
def test_parser_fails_closed_on_unsupported_shapes(response: ChatCompletion) -> None:
    with pytest.raises(ProviderAdapterError):
        parse_response(response, route=route())


def test_parser_classifies_refusal() -> None:
    with pytest.raises(ProviderAdapterError) as caught:
        parse_response(completion(content="No", refusal="blocked"), route=route())
    assert caught.value.failure.kind == ProviderFailureKind.CONTENT_REFUSAL


def test_request_selects_token_field_and_omits_fixed_sampling_temperature() -> None:
    normal = resolve_runtime_route(
        "openai/gpt-4.1", environment={}, max_output_tokens=32, temperature=0.2
    )
    normal_request = build_request(normal, request(UserMessage(text="Hello")))
    assert normal_request["messages"][0] == {
        "role": "system",
        "content": build_system_prompt().text,
    }
    assert normal_request["messages"][1] == {"role": "user", "content": "Hello"}
    assert normal_request["max_tokens"] == 32
    assert normal_request["temperature"] == 0.2
    assert "max_completion_tokens" not in normal_request

    reasoning = resolve_runtime_route(
        "openai/gpt-5", environment={}, max_output_tokens=64, temperature=0.2
    )
    reasoning_request = build_request(reasoning, request(UserMessage(text="Hello")))
    assert reasoning_request["max_completion_tokens"] == 64
    assert "max_tokens" not in reasoning_request
    assert "temperature" not in reasoning_request


def test_openrouter_preserves_nested_wire_slug_and_custom_preserves_model() -> None:
    openrouter = resolve_runtime_route("openrouter/anthropic/claude-opus-4-8", environment={})
    assert build_request(openrouter, request(UserMessage(text="Hi")))["model"] == (
        "anthropic/claude-opus-4-8"
    )

    custom = resolve_runtime_route(
        "vendor/model",
        environment={},
        custom_protocol="openai-compatible",
        custom_base_url="https://gateway.example/v1",
    )
    assert build_request(custom, request(UserMessage(text="Hi")))["model"] == "vendor/model"


def test_request_body_limit_fails_before_client_call() -> None:
    limited_definition = replace(OPENAI, request_body_limit=50)
    limited_route = replace(route(), definition=limited_definition)
    client = RecordingChatClient([completion(content="unused")])
    provider = OpenAICompatibleConversationProvider(limited_route, client)

    with pytest.raises(ProviderAdapterError) as caught:
        provider.respond(request(UserMessage(text="a long enough message to cross the body limit")))
    assert caught.value.failure.kind == ProviderFailureKind.INVALID_REQUEST
    assert client.requests == []


def test_adapter_backed_loop_preserves_atomic_tool_causality(tmp_path) -> None:
    (tmp_path / "README.md").write_text("workspace notes\n", encoding="utf-8")
    client = RecordingChatClient(
        [
            completion(finish_reason="tool_calls", tool_calls=[tool_call(call_id="call_read")]),
            completion(content="I read it."),
        ]
    )
    loop = AgentLoop(OpenAICompatibleConversationProvider(route(), client), ReadFileTool(tmp_path))

    assert loop.run("Read README") == "I read it."
    assert client.requests[1]["messages"][0] == {
        "role": "system",
        "content": build_system_prompt().text,
    }
    assert sum(message["role"] == "system" for message in client.requests[1]["messages"]) == 1
    assert client.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_read",
        "content": "workspace notes\n",
    }
