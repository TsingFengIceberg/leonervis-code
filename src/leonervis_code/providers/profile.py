"""Validated non-secret named provider profiles for the local runtime."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from collections.abc import Mapping

from leonervis_code.core.orchestration import OrchestrationError
from leonervis_code.providers.definitions import BUILTIN_PROVIDERS, WireProtocol
from leonervis_code.providers.resolver import (
    normalize_compatible_base_url,
    valid_environment_name,
    validate_base_url,
)

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MAX_MODEL_LENGTH = 512
MAX_BASE_URL_LENGTH = 2048
MAX_ENVIRONMENT_NAME_LENGTH = 128
_PROFILE_FIELDS = {
    "name",
    "provider_id",
    "protocol",
    "model",
    "base_url",
    "api_key_env",
    "max_output_tokens",
    "temperature",
}
_REQUIRED_PROFILE_FIELDS = {"name", "provider_id", "protocol", "model"}


class ProviderProfileError(OrchestrationError):
    """Raised when named provider profile data is invalid or unsafe."""


@dataclass(frozen=True)
class NamedProviderProfile:
    """One reusable endpoint/model profile that never contains a credential value."""

    name: str
    provider_id: str
    protocol: WireProtocol
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    max_output_tokens: int = 1024
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _PROFILE_NAME.fullmatch(self.name) is None:
            raise ProviderProfileError(
                "profile name must be 1-64 ASCII letters, digits, dots, underscores, or hyphens"
            )
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ProviderProfileError("profile provider ID must not be blank")
        if self.provider_id != self.provider_id.strip().lower():
            raise ProviderProfileError("profile provider ID must be lowercase without whitespace")
        if not isinstance(self.protocol, WireProtocol):
            raise ProviderProfileError("profile protocol is invalid")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ProviderProfileError("profile model must not be blank")
        if self.model != self.model.strip():
            raise ProviderProfileError("profile model must not have surrounding whitespace")
        if len(self.model) > MAX_MODEL_LENGTH:
            raise ProviderProfileError(
                f"profile model must not exceed {MAX_MODEL_LENGTH} characters"
            )
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ProviderProfileError("profile max output tokens must be a positive integer")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
                raise ProviderProfileError("profile temperature must be a number")
            if not math.isfinite(float(self.temperature)) or not 0.0 <= self.temperature <= 2.0:
                raise ProviderProfileError("profile temperature must be between 0.0 and 2.0")
        if self.api_key_env is not None:
            if not isinstance(self.api_key_env, str) or not self.api_key_env.strip():
                raise ProviderProfileError("profile API key environment variable must not be blank")
            if len(self.api_key_env) > MAX_ENVIRONMENT_NAME_LENGTH:
                raise ProviderProfileError("profile API key environment variable name is too long")
            if not valid_environment_name(self.api_key_env):
                raise ProviderProfileError("profile API key environment variable name is invalid")

        definition = BUILTIN_PROVIDERS.get(self.provider_id)
        if definition is not None:
            if self.protocol != definition.protocol:
                raise ProviderProfileError(
                    f"profile protocol does not match built-in provider {self.provider_id}"
                )
        elif self.provider_id == "custom":
            if self.protocol != WireProtocol.OPENAI_CHAT_COMPLETIONS:
                raise ProviderProfileError("custom profiles require an OpenAI-compatible protocol")
            if self.base_url is None:
                raise ProviderProfileError("custom profiles require a base URL")
        else:
            raise ProviderProfileError(f"unknown profile provider: {self.provider_id}")

        if self.base_url is not None:
            if not isinstance(self.base_url, str) or not self.base_url.strip():
                raise ProviderProfileError("profile base URL must not be blank")
            if len(self.base_url) > MAX_BASE_URL_LENGTH:
                raise ProviderProfileError(
                    f"profile base URL must not exceed {MAX_BASE_URL_LENGTH} characters"
                )
            try:
                if self.protocol == WireProtocol.OPENAI_CHAT_COMPLETIONS:
                    normalized = normalize_compatible_base_url(self.base_url)
                else:
                    normalized = validate_base_url(self.base_url)
            except OrchestrationError as error:
                raise ProviderProfileError(str(error)) from None
            object.__setattr__(self, "base_url", normalized)
        if self.temperature is not None:
            object.__setattr__(self, "temperature", float(self.temperature))

    def to_dict(self) -> dict[str, object]:
        """Return the complete version-independent JSON representation."""
        return {
            "name": self.name,
            "provider_id": self.provider_id,
            "protocol": self.protocol.value,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> NamedProviderProfile:
        """Decode one closed profile object and reject unknown or missing fields."""
        if not isinstance(value, Mapping):
            raise ProviderProfileError("profile entry must be a JSON object")
        fields = set(value)
        unknown = fields - _PROFILE_FIELDS
        if unknown:
            raise ProviderProfileError(f"profile contains unknown field: {sorted(unknown)[0]}")
        missing = _REQUIRED_PROFILE_FIELDS - fields
        if missing:
            raise ProviderProfileError(f"profile is missing required field: {sorted(missing)[0]}")

        protocol_value = value["protocol"]
        if not isinstance(protocol_value, str):
            raise ProviderProfileError("profile protocol must be text")
        try:
            protocol = WireProtocol(protocol_value)
        except ValueError:
            raise ProviderProfileError(f"unsupported profile protocol: {protocol_value}") from None

        name = value["name"]
        provider_id = value["provider_id"]
        model = value["model"]
        base_url = value.get("base_url")
        api_key_env = value.get("api_key_env")
        max_output_tokens = value.get("max_output_tokens", 1024)
        temperature = value.get("temperature")
        if not isinstance(name, str):
            raise ProviderProfileError("profile name must be text")
        if not isinstance(provider_id, str):
            raise ProviderProfileError("profile provider ID must be text")
        if not isinstance(model, str):
            raise ProviderProfileError("profile model must be text")
        if base_url is not None and not isinstance(base_url, str):
            raise ProviderProfileError("profile base URL must be text or null")
        if api_key_env is not None and not isinstance(api_key_env, str):
            raise ProviderProfileError("profile API key environment variable must be text or null")
        return cls(
            name=name,
            provider_id=provider_id,
            protocol=protocol,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            max_output_tokens=max_output_tokens,  # type: ignore[arg-type]
            temperature=temperature,  # type: ignore[arg-type]
        )
