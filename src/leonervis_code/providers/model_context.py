"""Provider-owned model context capability contracts and resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from leonervis_code.providers.definitions import RuntimeProviderRoute, WireProtocol

MAX_CONTEXT_WINDOW_TOKENS = 100_000_000
OFFICIAL_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class ModelContextSource(StrEnum):
    """The authority used for one resolved context-window value."""

    PROFILE_OVERRIDE = "profile_override"
    BUILTIN_CATALOG = "builtin_catalog"
    DISCOVERY_CACHE = "discovery_cache"
    LIVE_DISCOVERY = "live_discovery"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelContextTarget:
    """A non-secret exact provider deployment and model identity."""

    provider_id: str
    protocol: WireProtocol
    base_url: str
    wire_model: str
    credential_env: str | None

    @classmethod
    def from_route(cls, route: RuntimeProviderRoute) -> ModelContextTarget:
        definition = route.definition
        return cls(
            provider_id=definition.provider_id,
            protocol=definition.protocol,
            base_url=route.base_url,
            wire_model=route.wire_model,
            credential_env=definition.credential_env,
        )


@dataclass(frozen=True)
class ModelContextCapability:
    """One immutable context-window resolution snapshot."""

    target: ModelContextTarget | None
    context_window_tokens: int | None
    source: ModelContextSource
    discovered_at: str | None = None
    expires_at: str | None = None
    diagnostic: str | None = None

    @classmethod
    def unknown(
        cls,
        target: ModelContextTarget | None,
        *,
        diagnostic: str | None = None,
    ) -> ModelContextCapability:
        return cls(
            target=target,
            context_window_tokens=None,
            source=ModelContextSource.UNKNOWN,
            diagnostic=diagnostic,
        )


@dataclass(frozen=True)
class ModelContextDiscovery:
    """A provider-owned live discovery result before cache attribution."""

    context_window_tokens: int | None
    diagnostic: str | None = None


class ModelContextDiscoverer(Protocol):
    """The optional narrow capability exposed by a provider adapter."""

    def discover_model_context(self) -> ModelContextDiscovery:
        """Return a positive discovered limit or an unknown diagnostic."""


class ModelContextCache(Protocol):
    """The cache operations used by the resolver."""

    def get(
        self, target: ModelContextTarget, *, now: datetime
    ) -> tuple[ModelContextCapability | None, str | None]:
        """Return a fresh cached capability and an optional safe diagnostic."""

    def put(
        self,
        target: ModelContextTarget,
        context_window_tokens: int,
        *,
        now: datetime,
    ) -> str | None:
        """Persist one positive discovery and return an optional safe diagnostic."""


# Reviewed against official provider metadata on 2026-07-20. These entries
# deliberately match only the official endpoint and exact wire model string.
_BUILTIN_CONTEXT_WINDOWS: Mapping[tuple[str, str, str], int] = {
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-fable-5"): 1_000_000,
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-mythos-5"): 1_000_000,
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-opus-4-8"): 1_000_000,
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-opus-4-7"): 1_000_000,
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-opus-4-6"): 1_000_000,
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-sonnet-5"): 1_000_000,
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-sonnet-4-6"): 1_000_000,
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-haiku-4-5"): 200_000,
    (
        "anthropic",
        OFFICIAL_ANTHROPIC_BASE_URL,
        "claude-haiku-4-5-20251001",
    ): 200_000,
}


class ModelContextCapabilityResolver:
    """Resolve exact model context facts without inventing unknown limits."""

    def __init__(
        self,
        cache: ModelContextCache | None = None,
        *,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._cache = cache
        self._clock = clock

    def resolve(
        self,
        route: RuntimeProviderRoute,
        *,
        profile_override: int | None = None,
        discoverer: object | None = None,
    ) -> ModelContextCapability:
        target = ModelContextTarget.from_route(route)
        if profile_override is not None:
            _validate_context_window(profile_override)
            return ModelContextCapability(
                target=target,
                context_window_tokens=profile_override,
                source=ModelContextSource.PROFILE_OVERRIDE,
            )

        catalog_value = builtin_context_window(target)
        if catalog_value is not None:
            return ModelContextCapability(
                target=target,
                context_window_tokens=catalog_value,
                source=ModelContextSource.BUILTIN_CATALOG,
            )

        cache_diagnostic = None
        now = self._clock()
        if self._cache is not None:
            cached, cache_diagnostic = self._cache.get(target, now=now)
            if cached is not None:
                return cached

        operation = getattr(discoverer, "discover_model_context", None)
        if not live_discovery_eligible(target) or not callable(operation):
            return ModelContextCapability.unknown(
                target,
                diagnostic=_join_diagnostics(
                    cache_diagnostic, "live context discovery is unsupported"
                ),
            )

        try:
            discovered = operation()
        except Exception:
            return ModelContextCapability.unknown(
                target,
                diagnostic=_join_diagnostics(
                    cache_diagnostic, "live context discovery failed safely"
                ),
            )
        if not isinstance(discovered, ModelContextDiscovery):
            return ModelContextCapability.unknown(
                target,
                diagnostic=_join_diagnostics(
                    cache_diagnostic, "live context discovery returned an invalid result"
                ),
            )
        if discovered.context_window_tokens is None:
            return ModelContextCapability.unknown(
                target,
                diagnostic=_join_diagnostics(cache_diagnostic, discovered.diagnostic),
            )
        try:
            _validate_context_window(discovered.context_window_tokens)
        except ValueError:
            return ModelContextCapability.unknown(
                target,
                diagnostic=_join_diagnostics(
                    cache_diagnostic, "live context discovery returned an invalid limit"
                ),
            )

        write_diagnostic = None
        if self._cache is not None:
            write_diagnostic = self._cache.put(target, discovered.context_window_tokens, now=now)
        return ModelContextCapability(
            target=target,
            context_window_tokens=discovered.context_window_tokens,
            source=ModelContextSource.LIVE_DISCOVERY,
            discovered_at=_format_time(now),
            diagnostic=_join_diagnostics(cache_diagnostic, write_diagnostic),
        )

    def resolve_offline(
        self,
        route: RuntimeProviderRoute,
        *,
        profile_override: int | None = None,
    ) -> ModelContextCapability:
        """Resolve only explicit/static facts without cache or network access."""
        target = ModelContextTarget.from_route(route)
        if profile_override is not None:
            _validate_context_window(profile_override)
            return ModelContextCapability(
                target=target,
                context_window_tokens=profile_override,
                source=ModelContextSource.PROFILE_OVERRIDE,
            )
        catalog_value = builtin_context_window(target)
        if catalog_value is not None:
            return ModelContextCapability(
                target=target,
                context_window_tokens=catalog_value,
                source=ModelContextSource.BUILTIN_CATALOG,
            )
        diagnostic = (
            "live discovery is available at runtime"
            if live_discovery_eligible(target)
            else "live context discovery is unsupported"
        )
        return ModelContextCapability.unknown(target, diagnostic=diagnostic)


def builtin_context_window(target: ModelContextTarget) -> int | None:
    """Return one exact reviewed catalog value, never a family guess."""
    return _BUILTIN_CONTEXT_WINDOWS.get((target.provider_id, target.base_url, target.wire_model))


def live_discovery_eligible(target: ModelContextTarget) -> bool:
    """Return whether the current slice permits a live Models API lookup."""
    return (
        target.provider_id == "anthropic"
        and target.protocol == WireProtocol.ANTHROPIC_MESSAGES
        and target.base_url == OFFICIAL_ANTHROPIC_BASE_URL
    )


def _validate_context_window(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_CONTEXT_WINDOW_TOKENS:
        raise ValueError(f"context window tokens must be between 1 and {MAX_CONTEXT_WINDOW_TOKENS}")


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _join_diagnostics(*values: str | None) -> str | None:
    parts = [value for value in values if value]
    return "; ".join(parts) if parts else None
