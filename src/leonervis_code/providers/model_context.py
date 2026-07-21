"""Provider-owned model context capability contracts and resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from leonervis_code.providers.definitions import RuntimeProviderRoute, WireProtocol

MAX_CONTEXT_WINDOW_TOKENS = 100_000_000
MAX_MODEL_OUTPUT_TOKENS = 100_000_000
OFFICIAL_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class ModelContextSource(StrEnum):
    """The authority used for one resolved model-limit value."""

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
    """One immutable resolution snapshot for context and model-output limits."""

    target: ModelContextTarget | None
    context_window_tokens: int | None
    source: ModelContextSource
    discovered_at: str | None = None
    expires_at: str | None = None
    diagnostic: str | None = None
    model_max_output_tokens: int | None = None
    model_max_output_source: ModelContextSource = ModelContextSource.UNKNOWN
    model_max_output_discovered_at: str | None = None
    model_max_output_expires_at: str | None = None
    model_max_output_diagnostic: str | None = None

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
            model_max_output_diagnostic=diagnostic,
        )


@dataclass(frozen=True)
class ModelContextDiscovery:
    """Provider-owned live discovery facts before cache attribution."""

    context_window_tokens: int | None
    diagnostic: str | None = None
    model_max_output_tokens: int | None = None


class ModelContextDiscoverer(Protocol):
    """The optional narrow capability exposed by a provider adapter."""

    def discover_model_context(self) -> ModelContextDiscovery:
        """Return independently discovered positive limits or unknown diagnostics."""


class ModelContextCache(Protocol):
    """The cache operations used by the resolver."""

    def get(
        self, target: ModelContextTarget, *, now: datetime
    ) -> tuple[ModelContextCapability | None, str | None]:
        """Return fresh cached facts and an optional safe diagnostic."""

    def put(
        self,
        target: ModelContextTarget,
        context_window_tokens: int | None,
        model_max_output_tokens: int | None,
        *,
        now: datetime,
    ) -> str | None:
        """Persist positive discovery facts and return an optional diagnostic."""


# Reviewed against official provider metadata on 2026-07-21. Entries match
# only the official endpoint and exact wire model string. Values are
# (context window, model maximum output).
_BUILTIN_MODEL_LIMITS: Mapping[tuple[str, str, str], tuple[int, int]] = {
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-fable-5"): (1_000_000, 128_000),
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-mythos-5"): (1_000_000, 128_000),
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-opus-4-8"): (1_000_000, 128_000),
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-opus-4-7"): (1_000_000, 128_000),
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-opus-4-6"): (1_000_000, 128_000),
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-sonnet-5"): (1_000_000, 128_000),
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-sonnet-4-6"): (1_000_000, 128_000),
    ("anthropic", OFFICIAL_ANTHROPIC_BASE_URL, "claude-haiku-4-5"): (200_000, 64_000),
    (
        "anthropic",
        OFFICIAL_ANTHROPIC_BASE_URL,
        "claude-haiku-4-5-20251001",
    ): (200_000, 64_000),
}


class ModelContextCapabilityResolver:
    """Resolve exact model limits without inventing unknown facts."""

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
        model_max_output_override: int | None = None,
        discoverer: object | None = None,
    ) -> ModelContextCapability:
        target = ModelContextTarget.from_route(route)
        capability = self._static_capability(
            target,
            profile_override=profile_override,
            model_max_output_override=model_max_output_override,
        )
        if _complete(capability):
            return capability

        cache_diagnostic = None
        now = self._clock()
        if self._cache is not None:
            cached, cache_diagnostic = self._cache.get(target, now=now)
            if cached is not None:
                capability = _fill_unknown(capability, cached)
                if _complete(capability):
                    return capability

        operation = getattr(discoverer, "discover_model_context", None)
        if not live_discovery_eligible(target) or not callable(operation):
            return _with_unknown_diagnostic(
                capability,
                _join_diagnostics(cache_diagnostic, "live context discovery is unsupported"),
            )

        try:
            discovered = operation()
        except Exception:
            return _with_unknown_diagnostic(
                capability,
                _join_diagnostics(cache_diagnostic, "live context discovery failed safely"),
            )
        if not isinstance(discovered, ModelContextDiscovery):
            return _with_unknown_diagnostic(
                capability,
                _join_diagnostics(
                    cache_diagnostic, "live context discovery returned an invalid result"
                ),
            )

        context_value, context_error = _validated_discovery_value(
            discovered.context_window_tokens, _validate_context_window, "context window"
        )
        output_value, output_error = _validated_discovery_value(
            discovered.model_max_output_tokens,
            _validate_model_max_output,
            "model max output",
        )
        write_diagnostic = None
        if self._cache is not None and (context_value is not None or output_value is not None):
            write_diagnostic = self._cache.put(
                target,
                context_value,
                output_value,
                now=now,
            )
        live_time = _format_time(now)
        live = ModelContextCapability(
            target=target,
            context_window_tokens=context_value,
            source=(
                ModelContextSource.LIVE_DISCOVERY
                if context_value is not None
                else ModelContextSource.UNKNOWN
            ),
            discovered_at=live_time if context_value is not None else None,
            diagnostic=_join_diagnostics(discovered.diagnostic, context_error),
            model_max_output_tokens=output_value,
            model_max_output_source=(
                ModelContextSource.LIVE_DISCOVERY
                if output_value is not None
                else ModelContextSource.UNKNOWN
            ),
            model_max_output_discovered_at=live_time if output_value is not None else None,
            model_max_output_diagnostic=_join_diagnostics(discovered.diagnostic, output_error),
        )
        resolved = _fill_unknown(capability, live)
        return _append_diagnostic(resolved, _join_diagnostics(cache_diagnostic, write_diagnostic))

    def resolve_offline(
        self,
        route: RuntimeProviderRoute,
        *,
        profile_override: int | None = None,
        model_max_output_override: int | None = None,
    ) -> ModelContextCapability:
        """Resolve only explicit/static facts without cache or network access."""
        target = ModelContextTarget.from_route(route)
        capability = self._static_capability(
            target,
            profile_override=profile_override,
            model_max_output_override=model_max_output_override,
        )
        if _complete(capability):
            return capability
        diagnostic = (
            "live discovery is available at runtime"
            if live_discovery_eligible(target)
            else "live context discovery is unsupported"
        )
        return _with_unknown_diagnostic(capability, diagnostic)

    @staticmethod
    def _static_capability(
        target: ModelContextTarget,
        *,
        profile_override: int | None,
        model_max_output_override: int | None,
    ) -> ModelContextCapability:
        if profile_override is not None:
            _validate_context_window(profile_override)
        if model_max_output_override is not None:
            _validate_model_max_output(model_max_output_override)
        catalog = builtin_model_limits(target)
        context_value = profile_override
        context_source = (
            ModelContextSource.PROFILE_OVERRIDE
            if context_value is not None
            else ModelContextSource.UNKNOWN
        )
        output_value = model_max_output_override
        output_source = (
            ModelContextSource.PROFILE_OVERRIDE
            if output_value is not None
            else ModelContextSource.UNKNOWN
        )
        if catalog is not None:
            if context_value is None:
                context_value = catalog[0]
                context_source = ModelContextSource.BUILTIN_CATALOG
            if output_value is None:
                output_value = catalog[1]
                output_source = ModelContextSource.BUILTIN_CATALOG
        return ModelContextCapability(
            target=target,
            context_window_tokens=context_value,
            source=context_source,
            model_max_output_tokens=output_value,
            model_max_output_source=output_source,
        )


def builtin_model_limits(target: ModelContextTarget) -> tuple[int, int] | None:
    """Return exact reviewed catalog limits, never a family guess."""
    return _BUILTIN_MODEL_LIMITS.get((target.provider_id, target.base_url, target.wire_model))


def builtin_context_window(target: ModelContextTarget) -> int | None:
    """Return one exact reviewed context value for backward-compatible callers."""
    limits = builtin_model_limits(target)
    return limits[0] if limits is not None else None


def live_discovery_eligible(target: ModelContextTarget) -> bool:
    """Return whether this slice permits a live Anthropic Models API lookup."""
    return (
        target.provider_id == "anthropic"
        and target.protocol == WireProtocol.ANTHROPIC_MESSAGES
        and target.base_url == OFFICIAL_ANTHROPIC_BASE_URL
    )


def _complete(capability: ModelContextCapability) -> bool:
    return (
        capability.context_window_tokens is not None
        and capability.model_max_output_tokens is not None
    )


def _fill_unknown(
    base: ModelContextCapability, fallback: ModelContextCapability
) -> ModelContextCapability:
    use_context = base.context_window_tokens is None
    use_output = base.model_max_output_tokens is None
    return ModelContextCapability(
        target=base.target,
        context_window_tokens=(
            fallback.context_window_tokens if use_context else base.context_window_tokens
        ),
        source=fallback.source if use_context else base.source,
        discovered_at=fallback.discovered_at if use_context else base.discovered_at,
        expires_at=fallback.expires_at if use_context else base.expires_at,
        diagnostic=fallback.diagnostic if use_context else base.diagnostic,
        model_max_output_tokens=(
            fallback.model_max_output_tokens if use_output else base.model_max_output_tokens
        ),
        model_max_output_source=(
            fallback.model_max_output_source if use_output else base.model_max_output_source
        ),
        model_max_output_discovered_at=(
            fallback.model_max_output_discovered_at
            if use_output
            else base.model_max_output_discovered_at
        ),
        model_max_output_expires_at=(
            fallback.model_max_output_expires_at if use_output else base.model_max_output_expires_at
        ),
        model_max_output_diagnostic=(
            fallback.model_max_output_diagnostic if use_output else base.model_max_output_diagnostic
        ),
    )


def _with_unknown_diagnostic(
    capability: ModelContextCapability, diagnostic: str | None
) -> ModelContextCapability:
    return ModelContextCapability(
        target=capability.target,
        context_window_tokens=capability.context_window_tokens,
        source=capability.source,
        discovered_at=capability.discovered_at,
        expires_at=capability.expires_at,
        diagnostic=(
            _join_diagnostics(capability.diagnostic, diagnostic)
            if capability.context_window_tokens is None
            else capability.diagnostic
        ),
        model_max_output_tokens=capability.model_max_output_tokens,
        model_max_output_source=capability.model_max_output_source,
        model_max_output_discovered_at=capability.model_max_output_discovered_at,
        model_max_output_expires_at=capability.model_max_output_expires_at,
        model_max_output_diagnostic=(
            _join_diagnostics(capability.model_max_output_diagnostic, diagnostic)
            if capability.model_max_output_tokens is None
            else capability.model_max_output_diagnostic
        ),
    )


def _append_diagnostic(
    capability: ModelContextCapability, diagnostic: str | None
) -> ModelContextCapability:
    if diagnostic is None:
        return capability
    return ModelContextCapability(
        target=capability.target,
        context_window_tokens=capability.context_window_tokens,
        source=capability.source,
        discovered_at=capability.discovered_at,
        expires_at=capability.expires_at,
        diagnostic=_join_diagnostics(capability.diagnostic, diagnostic),
        model_max_output_tokens=capability.model_max_output_tokens,
        model_max_output_source=capability.model_max_output_source,
        model_max_output_discovered_at=capability.model_max_output_discovered_at,
        model_max_output_expires_at=capability.model_max_output_expires_at,
        model_max_output_diagnostic=_join_diagnostics(
            capability.model_max_output_diagnostic, diagnostic
        ),
    )


def _validated_discovery_value(value, validator, label: str) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    try:
        validator(value)
    except ValueError:
        return None, f"live context discovery returned an invalid {label} limit"
    return value, None


def _validate_context_window(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_CONTEXT_WINDOW_TOKENS:
        raise ValueError(f"context window tokens must be between 1 and {MAX_CONTEXT_WINDOW_TOKENS}")


def _validate_model_max_output(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_MODEL_OUTPUT_TOKENS:
        raise ValueError(f"model max output tokens must be between 1 and {MAX_MODEL_OUTPUT_TOKENS}")


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _join_diagnostics(*values: str | None) -> str | None:
    parts = [value for value in values if value]
    return "; ".join(parts) if parts else None
