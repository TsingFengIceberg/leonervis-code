"""Construct a provider-neutral conversation client from one resolved runtime route."""

from __future__ import annotations

from collections.abc import Mapping

from leonervis_code.core.contracts import ConversationProvider
from leonervis_code.core.orchestration import ProviderFailureKind
from leonervis_code.providers.anthropic import (
    AnthropicProviderConfig,
    create_anthropic_provider,
)
from leonervis_code.providers.definitions import RuntimeProviderRoute, WireProtocol
from leonervis_code.providers.errors import ProviderAdapterError, adapter_error
from leonervis_code.providers.openai_compat import create_openai_compatible_provider


def create_provider(
    route: RuntimeProviderRoute,
    *,
    environment: Mapping[str, str],
) -> ConversationProvider:
    """Resolve the selected credential only while constructing its SDK client."""
    definition = route.definition
    api_key = None
    if definition.credential_env:
        api_key = environment.get(definition.credential_env, "")
    if definition.credential_required and not (api_key and api_key.strip()):
        raise _missing_credential(route)

    if definition.protocol == WireProtocol.ANTHROPIC_MESSAGES:
        return create_anthropic_provider(
            AnthropicProviderConfig(
                model_id=route.wire_model,
                max_output_tokens=route.max_output_tokens,
                base_url=route.base_url,
            ),
            api_key=api_key or "",
        )
    if definition.protocol == WireProtocol.OPENAI_CHAT_COMPLETIONS:
        return create_openai_compatible_provider(route, api_key=api_key)
    raise adapter_error(
        provider_id=definition.provider_id,
        model_id=route.selected_model,
        kind=ProviderFailureKind.INVALID_REQUEST,
        code="unsupported_protocol",
        message=f"unsupported provider protocol: {definition.protocol}",
    )


def _missing_credential(route: RuntimeProviderRoute) -> ProviderAdapterError:
    definition = route.definition
    return adapter_error(
        provider_id=definition.provider_id,
        model_id=route.selected_model,
        kind=ProviderFailureKind.AUTHENTICATION,
        code="missing_api_key",
        message=f"{definition.credential_env} is not configured",
    )
