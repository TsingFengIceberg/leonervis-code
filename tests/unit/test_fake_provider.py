import pytest

from leonervis_code.core.contracts import (
    AssistantText,
    ConversationRequest,
    ToolArguments,
    ToolUse,
    UserMessage,
)
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.system_prompt import build_system_prompt


def request(*history):
    return ConversationRequest(system_prompt=build_system_prompt(), history=tuple(history))


def test_default_fake_provider_uses_the_latest_user_text() -> None:
    provider = ScriptedFakeProvider()
    model_request = request(
        UserMessage(text="Hello"),
        AssistantText(text="Fake response: Hello"),
        UserMessage(text="Again"),
    )

    assert provider.respond(model_request) == AssistantText(text="Fake response: Again")
    assert provider.received_requests == (model_request,)


def test_fake_provider_returns_scripted_outcomes_in_order() -> None:
    provider = ScriptedFakeProvider(
        [
            ToolUse(
                tool_use_id="read-1",
                name="read_file",
                arguments=ToolArguments.from_mapping({"path": "README.md"}),
            ),
            AssistantText(text="summary"),
        ]
    )
    first_request = request(UserMessage(text="Read README"))
    second_request = request(
        UserMessage(text="Read README"),
        ToolUse(
            tool_use_id="read-1",
            name="read_file",
            arguments=ToolArguments.from_mapping({"path": "README.md"}),
        ),
    )

    assert provider.respond(first_request) == ToolUse(
        tool_use_id="read-1",
        name="read_file",
        arguments=ToolArguments.from_mapping({"path": "README.md"}),
    )
    assert provider.respond(second_request) == AssistantText(text="summary")
    assert provider.received_requests == (first_request, second_request)


def test_fake_provider_raises_scripted_errors_and_exhaustion() -> None:
    provider = ScriptedFakeProvider([RuntimeError("planned failure")])
    model_request = request(UserMessage(text="Hello"))

    with pytest.raises(RuntimeError, match="planned failure"):
        provider.respond(model_request)
    with pytest.raises(RuntimeError, match="fake provider script is exhausted"):
        provider.respond(model_request)
