from leonervis_code.providers.fake import DeterministicFakeProvider


def test_fake_provider_returns_a_stable_response() -> None:
    provider = DeterministicFakeProvider()

    assert provider.respond("Hello") == "Fake response: Hello"
    assert provider.respond("Hello") == "Fake response: Hello"


def test_fake_provider_preserves_the_original_prompt() -> None:
    prompt = "  Keep these spaces.  "

    assert DeterministicFakeProvider().respond(prompt) == f"Fake response: {prompt}"
