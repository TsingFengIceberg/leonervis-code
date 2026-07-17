"""Deterministic real-provider selection without credential values or network I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from leonervis_code.core.orchestration import OrchestrationError
from leonervis_code.providers.definitions import (
    ANTHROPIC,
    BUILTIN_PROVIDERS,
    DASHSCOPE,
    OPENAI,
    XAI,
    ProviderDefinition,
    RuntimeProviderRoute,
    WireProtocol,
)

if TYPE_CHECKING:
    from leonervis_code.providers.profile import ProviderProfileSpec


class RuntimeRouteError(OrchestrationError):
    """Raised when an explicit real-provider route is invalid or ambiguous."""


def resolve_runtime_route(
    selector: str,
    *,
    environment: Mapping[str, str],
    max_output_tokens: int = 1024,
    temperature: float | None = None,
    custom_protocol: str | None = None,
    custom_base_url: str | None = None,
    custom_api_key_env: str | None = None,
) -> RuntimeProviderRoute:
    """Resolve one explicit provider/model selector to non-secret adapter metadata."""
    if not selector.strip():
        raise RuntimeRouteError("model selector must not be blank")
    if max_output_tokens < 1:
        raise RuntimeRouteError("max output tokens must be at least 1")
    if temperature is not None and not 0.0 <= temperature <= 2.0:
        raise RuntimeRouteError("temperature must be between 0.0 and 2.0")

    custom_values = (custom_protocol, custom_base_url, custom_api_key_env)
    if any(value is not None for value in custom_values):
        if custom_protocol != "openai-compatible" or custom_base_url is None:
            raise RuntimeRouteError(
                "custom providers require --provider-protocol openai-compatible and --base-url"
            )
        if custom_api_key_env is not None and not custom_api_key_env.strip():
            raise RuntimeRouteError("custom API key environment variable must not be blank")
        if custom_api_key_env is not None and not valid_environment_name(custom_api_key_env):
            raise RuntimeRouteError("custom API key environment variable name is invalid")
        definition = ProviderDefinition(
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            credential_env=custom_api_key_env,
            credential_required=custom_api_key_env is not None,
            default_base_url=normalize_compatible_base_url(custom_base_url),
        )
        return _route(
            definition,
            selector,
            selector,
            definition.default_base_url,
            "cli",
            max_output_tokens,
            temperature,
        )

    provider_id, separator, remainder = selector.partition("/")
    if separator:
        definition = BUILTIN_PROVIDERS.get(provider_id)
        if definition is None:
            raise RuntimeRouteError(f"unknown provider namespace: {provider_id}")
        if not remainder:
            raise RuntimeRouteError("model selector must include a model after the provider")
        wire_model = remainder
        return _route_for_definition(
            definition,
            selector,
            wire_model,
            environment,
            max_output_tokens,
            temperature,
        )

    definition = _definition_for_bare_model(selector)
    if definition is None:
        raise RuntimeRouteError(
            "unqualified model is not recognized; use an explicit provider/model selector"
        )
    return _route_for_definition(
        definition,
        selector,
        selector,
        environment,
        max_output_tokens,
        temperature,
    )


def resolve_profile_route(
    profile: ProviderProfileSpec,
    *,
    environment: Mapping[str, str],
    model_override: str | None = None,
) -> RuntimeProviderRoute:
    """Resolve one validated named profile to non-secret adapter metadata."""
    model = model_override if model_override is not None else profile.model
    if not model.strip():
        raise RuntimeRouteError("model selector must not be blank")
    if model != model.strip():
        raise RuntimeRouteError("model selector must not have surrounding whitespace")

    if profile.provider_id == "custom":
        assert profile.base_url is not None
        definition = ProviderDefinition(
            provider_id="custom",
            protocol=profile.protocol,
            credential_env=profile.api_key_env,
            credential_required=profile.api_key_env is not None,
            default_base_url=profile.base_url,
        )
        return _route(
            definition,
            model,
            model,
            profile.base_url,
            "profile",
            profile.max_output_tokens,
            profile.temperature,
        )

    definition = BUILTIN_PROVIDERS[profile.provider_id]
    if profile.api_key_env is not None:
        definition = replace(
            definition,
            credential_env=profile.api_key_env,
            credential_required=True,
        )
    wire_model = _wire_model_for_profile(profile.provider_id, model)
    if profile.base_url is not None:
        base_url = profile.base_url
        source = "profile"
    else:
        base_url = definition.default_base_url
        source = "default"
        if definition.base_url_env:
            configured = environment.get(definition.base_url_env, "").strip()
            if configured:
                base_url = configured
                source = "environment"
    if definition.protocol == WireProtocol.OPENAI_CHAT_COMPLETIONS:
        base_url = normalize_compatible_base_url(base_url)
    else:
        base_url = validate_base_url(base_url)
    return _route(
        definition,
        model,
        wire_model,
        base_url,
        source,
        profile.max_output_tokens,
        profile.temperature,
    )


def _wire_model_for_profile(provider_id: str, model: str) -> str:
    prefix = f"{provider_id}/"
    if provider_id != "openrouter" and model.startswith(prefix):
        wire_model = model[len(prefix) :]
        if not wire_model:
            raise RuntimeRouteError("model selector must include a model after the provider")
        return wire_model
    if provider_id == "openrouter" and model.startswith(prefix):
        wire_model = model[len(prefix) :]
        if not wire_model:
            raise RuntimeRouteError("model selector must include a model after the provider")
        return wire_model
    return model


def _definition_for_bare_model(model: str) -> ProviderDefinition | None:
    lowered = model.lower()
    if lowered.startswith("claude-"):
        return ANTHROPIC
    if lowered.startswith("gpt-") or lowered.startswith(("o1", "o3", "o4")):
        return OPENAI
    if lowered.startswith("grok-"):
        return XAI
    if lowered.startswith(("qwen-", "kimi-")):
        return DASHSCOPE
    return None


def _route_for_definition(
    definition: ProviderDefinition,
    selected_model: str,
    wire_model: str,
    environment: Mapping[str, str],
    max_output_tokens: int,
    temperature: float | None,
) -> RuntimeProviderRoute:
    base_url = definition.default_base_url
    source = "default"
    if definition.base_url_env:
        configured = environment.get(definition.base_url_env, "").strip()
        if configured:
            base_url = configured
            source = "environment"
    if definition.protocol == WireProtocol.OPENAI_CHAT_COMPLETIONS:
        base_url = normalize_compatible_base_url(base_url)
    else:
        base_url = validate_base_url(base_url)
    return _route(
        definition,
        selected_model,
        wire_model,
        base_url,
        source,
        max_output_tokens,
        temperature,
    )


def _route(
    definition: ProviderDefinition,
    selected_model: str,
    wire_model: str,
    base_url: str,
    base_url_source: str,
    max_output_tokens: int,
    temperature: float | None,
) -> RuntimeProviderRoute:
    return RuntimeProviderRoute(
        definition=definition,
        selected_model=selected_model,
        wire_model=wire_model,
        base_url=base_url,
        base_url_source=base_url_source,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )


def normalize_compatible_base_url(value: str) -> str:
    """Validate and normalize an OpenAI-compatible API base URL."""
    validated = validate_base_url(value)
    parsed = urlparse(validated)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return validated[: -len("/chat/completions")].rstrip("/")
    if path.endswith("/v1") or path.endswith("/api/v1"):
        return validated.rstrip("/")
    return f"{validated.rstrip('/')}/v1"


def valid_environment_name(value: str) -> bool:
    """Return whether a value is a portable ASCII environment-variable name."""
    return (
        bool(value)
        and (value[0].isascii() and (value[0].isalpha() or value[0] == "_"))
        and all(
            character.isascii() and (character.isalnum() or character == "_") for character in value
        )
    )


def validate_base_url(value: str) -> str:
    """Validate one absolute credential-free HTTP(S) provider base URL."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeRouteError("provider base URL must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeRouteError(
            "provider base URL must not contain credentials, query, or fragment"
        )
    return value.rstrip("/")
