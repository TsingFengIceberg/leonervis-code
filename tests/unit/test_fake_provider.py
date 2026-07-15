import pytest

from leonervis_code.core.contracts import TextMessage
from leonervis_code.providers.fake import ScriptedFakeProvider


def test_default_fake_provider_uses_the_latest_user_text() -> None:
    provider = ScriptedFakeProvider()
    history = (
        TextMessage(role="user", text="Hello"),
        TextMessage(role="assistant", text="Fake response: Hello"),
        TextMessage(role="user", text="Again"),
    )

    assert provider.respond(history) == "Fake response: Again"
    assert provider.received_histories == (history,)


def test_fake_provider_returns_scripted_outcomes_in_order() -> None:
    provider = ScriptedFakeProvider(["first", "second"])
    first_history = (TextMessage(role="user", text="one"),)
    second_history = (
        TextMessage(role="user", text="one"),
        TextMessage(role="assistant", text="first"),
        TextMessage(role="user", text="two"),
    )

    assert provider.respond(first_history) == "first"
    assert provider.respond(second_history) == "second"
    assert provider.received_histories == (first_history, second_history)


def test_fake_provider_raises_scripted_errors_and_exhaustion() -> None:
    provider = ScriptedFakeProvider([RuntimeError("planned failure")])
    history = (TextMessage(role="user", text="Hello"),)

    with pytest.raises(RuntimeError, match="planned failure"):
        provider.respond(history)
    with pytest.raises(RuntimeError, match="fake provider script is exhausted"):
        provider.respond(history)
