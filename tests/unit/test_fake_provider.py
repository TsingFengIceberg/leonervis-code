import pytest

from leonervis_code.core.contracts import AssistantText, ToolUse, UserMessage
from leonervis_code.providers.fake import ScriptedFakeProvider


def test_default_fake_provider_uses_the_latest_user_text() -> None:
    provider = ScriptedFakeProvider()
    history = (
        UserMessage(text="Hello"),
        AssistantText(text="Fake response: Hello"),
        UserMessage(text="Again"),
    )

    assert provider.respond(history) == AssistantText(text="Fake response: Again")
    assert provider.received_histories == (history,)


def test_fake_provider_returns_scripted_outcomes_in_order() -> None:
    provider = ScriptedFakeProvider(
        [
            ToolUse(tool_use_id="read-1", name="read_file", path="README.md"),
            AssistantText(text="summary"),
        ]
    )
    first_history = (UserMessage(text="Read README"),)
    second_history = first_history + (
        ToolUse(tool_use_id="read-1", name="read_file", path="README.md"),
    )

    assert provider.respond(first_history) == ToolUse(
        tool_use_id="read-1", name="read_file", path="README.md"
    )
    assert provider.respond(second_history) == AssistantText(text="summary")
    assert provider.received_histories == (first_history, second_history)


def test_fake_provider_raises_scripted_errors_and_exhaustion() -> None:
    provider = ScriptedFakeProvider([RuntimeError("planned failure")])
    history = (UserMessage(text="Hello"),)

    with pytest.raises(RuntimeError, match="planned failure"):
        provider.respond(history)
    with pytest.raises(RuntimeError, match="fake provider script is exhausted"):
        provider.respond(history)
