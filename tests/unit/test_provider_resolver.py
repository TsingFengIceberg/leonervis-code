from __future__ import annotations

import pytest

from leonervis_code.providers.definitions import ADAPTER_CONTRACT_VERSION, WireProtocol
from leonervis_code.providers.profile import NamedProviderProfile, ProviderProfileSpec
from leonervis_code.providers.resolver import (
    RuntimeRouteError,
    resolve_profile_route,
    resolve_runtime_route,
)


@pytest.mark.parametrize(
    ("selector", "provider", "protocol", "wire_model", "credential"),
    [
        (
            "anthropic/claude-opus-4-8",
            "anthropic",
            WireProtocol.ANTHROPIC_MESSAGES,
            "claude-opus-4-8",
            "ANTHROPIC_API_KEY",
        ),
        (
            "openai/gpt-5",
            "openai",
            WireProtocol.OPENAI_CHAT_COMPLETIONS,
            "gpt-5",
            "OPENAI_API_KEY",
        ),
        (
            "xai/grok-3",
            "xai",
            WireProtocol.OPENAI_CHAT_COMPLETIONS,
            "grok-3",
            "XAI_API_KEY",
        ),
        (
            "dashscope/qwen-plus",
            "dashscope",
            WireProtocol.OPENAI_CHAT_COMPLETIONS,
            "qwen-plus",
            "DASHSCOPE_API_KEY",
        ),
        (
            "ollama/qwen3:8b",
            "ollama",
            WireProtocol.OPENAI_CHAT_COMPLETIONS,
            "qwen3:8b",
            None,
        ),
        (
            "local/Qwen/Qwen3.5",
            "local",
            WireProtocol.OPENAI_CHAT_COMPLETIONS,
            "Qwen/Qwen3.5",
            None,
        ),
        (
            "openrouter/anthropic/claude-opus-4-8",
            "openrouter",
            WireProtocol.OPENAI_CHAT_COMPLETIONS,
            "anthropic/claude-opus-4-8",
            "OPENROUTER_API_KEY",
        ),
    ],
)
def test_explicit_builtin_selectors_resolve_without_credentials(
    selector: str,
    provider: str,
    protocol: WireProtocol,
    wire_model: str,
    credential: str | None,
) -> None:
    route = resolve_runtime_route(selector, environment={})

    assert route.definition.provider_id == provider
    assert route.definition.protocol == protocol
    assert route.definition.credential_env == credential
    assert route.wire_model == wire_model
    assert not hasattr(route, "api_key")


@pytest.mark.parametrize(
    ("selector", "provider"),
    [
        ("claude-opus-4-8", "anthropic"),
        ("gpt-5", "openai"),
        ("o3", "openai"),
        ("grok-3", "xai"),
        ("qwen-plus", "dashscope"),
        ("kimi-k2.5", "dashscope"),
    ],
)
def test_known_bare_models_have_deterministic_provider_families(
    selector: str, provider: str
) -> None:
    environment = {
        "ANTHROPIC_API_KEY": "must-not-influence-routing",
        "OPENAI_API_KEY": "must-not-influence-routing",
        "XAI_API_KEY": "must-not-influence-routing",
    }

    assert (
        resolve_runtime_route(selector, environment=environment).definition.provider_id == provider
    )


def test_unknown_namespaces_and_unqualified_models_fail_closed() -> None:
    with pytest.raises(RuntimeRouteError, match="unknown provider namespace"):
        resolve_runtime_route("gemini/model", environment={})
    with pytest.raises(RuntimeRouteError, match="unqualified model is not recognized"):
        resolve_runtime_route("mystery-model", environment={"OPENAI_API_KEY": "present"})


def test_base_url_overrides_are_validated_and_ollama_gets_one_v1_suffix() -> None:
    route = resolve_runtime_route(
        "ollama/qwen3:8b", environment={"OLLAMA_HOST": "http://127.0.0.1:11434"}
    )
    assert route.base_url == "http://127.0.0.1:11434/v1"
    assert route.base_url_source == "environment"

    complete = resolve_runtime_route(
        "ollama/qwen3:8b",
        environment={"OLLAMA_HOST": "http://127.0.0.1:11434/v1/chat/completions"},
    )
    assert complete.base_url == "http://127.0.0.1:11434/v1"

    with pytest.raises(RuntimeRouteError, match="must not contain credentials"):
        resolve_runtime_route(
            "openai/gpt-5", environment={"OPENAI_BASE_URL": "https://user:secret@example.test/v1"}
        )


def test_controlled_custom_endpoint_requires_explicit_protocol_and_base_url() -> None:
    with pytest.raises(RuntimeRouteError, match="custom providers require"):
        resolve_runtime_route(
            "vendor/model", environment={}, custom_base_url="https://example.test/v1"
        )

    with pytest.raises(RuntimeRouteError, match="variable name is invalid"):
        resolve_runtime_route(
            "vendor/model",
            environment={},
            custom_protocol="openai-compatible",
            custom_base_url="https://example.test/v1",
            custom_api_key_env="BAD-NAME",
        )

    route = resolve_runtime_route(
        "vendor/model",
        environment={},
        custom_protocol="openai-compatible",
        custom_base_url="https://example.test",
        custom_api_key_env="VENDOR_API_KEY",
    )
    assert route.definition.provider_id == "custom"
    assert route.wire_model == "vendor/model"
    assert route.definition.credential_env == "VENDOR_API_KEY"
    assert route.base_url == "https://example.test/v1"
    assert route.base_url_source == "cli"


def test_profile_resolver_is_identity_independent() -> None:
    spec = ProviderProfileSpec(
        name="local",
        provider_id="custom",
        protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
        model="vendor/model",
        base_url="https://gateway.example",
        api_key_env="VENDOR_API_KEY",
    )
    owned = NamedProviderProfile(
        **spec.__dict__,
        profile_id="00000000-0000-4000-8000-000000000001",
        revision=7,
    )

    assert resolve_profile_route(spec, environment={}) == resolve_profile_route(
        owned, environment={}
    )


def test_route_fingerprint_is_canonical_and_credential_state_independent() -> None:
    first = resolve_runtime_route("openai/gpt-5", environment={"OPENAI_API_KEY": "first"})
    second = resolve_runtime_route("openai/gpt-5", environment={"OPENAI_API_KEY": "second"})
    overridden = resolve_runtime_route(
        "openai/gpt-5",
        environment={"OPENAI_API_KEY": "first", "OPENAI_BASE_URL": "https://proxy.test/v1"},
    )

    assert ADAPTER_CONTRACT_VERSION == 4
    assert first.fingerprint() == second.fingerprint()
    assert len(first.fingerprint()) == 64
    assert first.fingerprint() != overridden.fingerprint()
