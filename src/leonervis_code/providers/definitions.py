"""Built-in real-provider definitions for the local Foundation 3B runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json


ADAPTER_CONTRACT_VERSION = 1


class WireProtocol(StrEnum):
    """The two wire-protocol families implemented by Leonervis Code."""

    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"


@dataclass(frozen=True)
class ProviderDefinition:
    """Non-secret transport and compatibility metadata for one provider route."""

    provider_id: str
    protocol: WireProtocol
    credential_env: str | None
    credential_required: bool
    default_base_url: str
    base_url_env: str | None = None
    request_body_limit: int = 100 * 1024 * 1024


@dataclass(frozen=True)
class RuntimeProviderRoute:
    """A resolved provider invocation plan that never contains a secret value."""

    definition: ProviderDefinition
    selected_model: str
    wire_model: str
    base_url: str
    base_url_source: str
    max_output_tokens: int = 1024
    temperature: float | None = None

    def fingerprint(self) -> str:
        """Return a canonical route hash excluding credential value and presence."""
        return route_fingerprint(self)


def route_fingerprint(route: RuntimeProviderRoute) -> str:
    """Return a canonical SHA-256 for one resolved adapter invocation contract."""
    payload = {
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "provider_id": route.definition.provider_id,
        "protocol": route.definition.protocol.value,
        "credential_env": route.definition.credential_env,
        "credential_required": route.definition.credential_required,
        "request_body_limit": route.definition.request_body_limit,
        "selected_model": route.selected_model,
        "wire_model": route.wire_model,
        "base_url": route.base_url,
        "base_url_source": route.base_url_source,
        "max_output_tokens": route.max_output_tokens,
        "temperature": route.temperature,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


ANTHROPIC = ProviderDefinition(
    provider_id="anthropic",
    protocol=WireProtocol.ANTHROPIC_MESSAGES,
    credential_env="ANTHROPIC_API_KEY",
    credential_required=True,
    default_base_url="https://api.anthropic.com",
)
OPENAI = ProviderDefinition(
    provider_id="openai",
    protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
    credential_env="OPENAI_API_KEY",
    credential_required=True,
    default_base_url="https://api.openai.com/v1",
    base_url_env="OPENAI_BASE_URL",
)
XAI = ProviderDefinition(
    provider_id="xai",
    protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
    credential_env="XAI_API_KEY",
    credential_required=True,
    default_base_url="https://api.x.ai/v1",
    base_url_env="XAI_BASE_URL",
    request_body_limit=50 * 1024 * 1024,
)
DASHSCOPE = ProviderDefinition(
    provider_id="dashscope",
    protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
    credential_env="DASHSCOPE_API_KEY",
    credential_required=True,
    default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    base_url_env="DASHSCOPE_BASE_URL",
    request_body_limit=6 * 1024 * 1024,
)
OLLAMA = ProviderDefinition(
    provider_id="ollama",
    protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
    credential_env=None,
    credential_required=False,
    default_base_url="http://127.0.0.1:11434/v1",
    base_url_env="OLLAMA_HOST",
)
LOCAL = ProviderDefinition(
    provider_id="local",
    protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
    credential_env=None,
    credential_required=False,
    default_base_url="http://127.0.0.1:11434/v1",
    base_url_env="OPENAI_BASE_URL",
)
OPENROUTER = ProviderDefinition(
    provider_id="openrouter",
    protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
    credential_env="OPENROUTER_API_KEY",
    credential_required=True,
    default_base_url="https://openrouter.ai/api/v1",
    base_url_env="OPENROUTER_BASE_URL",
)

BUILTIN_PROVIDERS: dict[str, ProviderDefinition] = {
    definition.provider_id: definition
    for definition in (ANTHROPIC, OPENAI, XAI, DASHSCOPE, OLLAMA, LOCAL, OPENROUTER)
}
