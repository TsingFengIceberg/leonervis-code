"""Provider-neutral contracts for offline model routing and request policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True)
class SecretRef:
    """An opaque credential handle that the routing layer must never resolve."""

    name: str


@dataclass(frozen=True)
class ProviderProfile:
    """Static provider availability metadata, without credential material."""

    provider_id: str
    adapter_key: str
    enabled: bool = True
    credential_ref: SecretRef | None = None


@dataclass(frozen=True)
class CapabilitySet:
    """The small set of model features relevant to Foundation 2 routing."""

    tool_use: bool = False
    streaming: bool = False
    system_messages: bool = False


class ParameterValueKind(StrEnum):
    """Supported canonical generation parameter value kinds."""

    INTEGER = "integer"
    FLOAT = "float"


class ParameterHandling(StrEnum):
    """How a selected adapter handles one valid canonical parameter."""

    REJECT = "reject"
    PASS_TO_ADAPTER = "pass_to_adapter"
    OMIT_WITH_DIAGNOSTIC = "omit_with_diagnostic"


@dataclass(frozen=True)
class ParameterSpec:
    """Validation and selected-model handling for one canonical option."""

    canonical_name: str
    value_kind: ParameterValueKind
    handling: ParameterHandling = ParameterHandling.PASS_TO_ADAPTER
    minimum: int | float | None = None
    maximum: int | float | None = None


@dataclass(frozen=True)
class ModelDefinition:
    """Offline metadata used to select and validate one provider model."""

    provider_id: str
    model_id: str
    aliases: tuple[str, ...]
    capabilities: CapabilitySet
    parameters: tuple[ParameterSpec, ...]


@dataclass(frozen=True)
class GenerationOptions:
    """Canonical optional generation controls understood by this slice."""

    max_output_tokens: int | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class RouteRequirements:
    """Capabilities the current caller requires from every route candidate."""

    requires_tool_use: bool = False
    requires_streaming: bool = False
    requires_system_messages: bool = False


@dataclass(frozen=True)
class RouteRequest:
    """One selected primary route, ordered fallbacks, and canonical options."""

    primary_selector: str
    fallback_selectors: tuple[str, ...] = ()
    requirements: RouteRequirements = RouteRequirements()
    options: GenerationOptions = GenerationOptions()
    extra_parameters: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class ProviderRequestPlan:
    """A selected route with validated canonical request options only."""

    provider_id: str
    adapter_key: str
    model_id: str
    canonical_parameters: tuple[tuple[str, int | float], ...]
    parameter_handling: tuple[tuple[str, ParameterHandling], ...]
    extra_parameters: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class ResolvedRoute:
    """A validated ordered primary-plus-fallback execution plan."""

    primary: ProviderRequestPlan
    fallbacks: tuple[ProviderRequestPlan, ...]


class ProviderDiagnosticSeverity(StrEnum):
    """User-visible severities for non-fatal compatibility actions."""

    INFO = "info"
    WARNING = "warning"


@dataclass(frozen=True)
class ProviderDiagnostic:
    """A stable, safe record of an adapter compatibility action."""

    code: str
    severity: ProviderDiagnosticSeverity
    message: str
    action: str


@dataclass(frozen=True)
class ProviderRequestPreview:
    """Pure provider-native preview produced by an adapter request transformer."""

    provider_id: str
    model_id: str
    native_parameters: tuple[tuple[str, int | float], ...]
    extra_parameters: tuple[tuple[str, object], ...]
    diagnostics: tuple[ProviderDiagnostic, ...]


class ProviderFailureKind(StrEnum):
    """Safe cross-provider failure classifications for future adapters."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_REQUEST = "invalid_request"
    MODEL_UNAVAILABLE = "model_unavailable"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RESPONSE_INVALID = "response_invalid"
    CONTENT_REFUSAL = "content_refusal"


@dataclass(frozen=True)
class ProviderFailure:
    """A redacted future-adapter failure with no provider raw response payload."""

    provider_id: str
    model_id: str
    kind: ProviderFailureKind
    diagnostic_code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = None
    request_id: str | None = None


class SecretResolver(Protocol):
    """Resolve a secret only inside a future real-provider adapter boundary."""

    def resolve(self, reference: SecretRef) -> str:
        """Return the secret identified by an opaque reference."""


class ProviderRequestTransformer(Protocol):
    """Convert a validated canonical route plan into a provider-native preview."""

    def preview(self, plan: ProviderRequestPlan) -> ProviderRequestPreview:
        """Apply provider-specific request policy without I/O or secrets."""


class ProviderAdapterFactory(Protocol):
    """Construct a future provider adapter from a prevalidated route."""

    def create(self, route: ResolvedRoute) -> object:
        """Create an adapter without exposing credentials to the route planner."""


class OrchestrationError(ValueError):
    """Base class for safe offline route-planning failures."""


class UnknownModelSelectorError(OrchestrationError):
    """Raised when a model selector matches no catalog entry."""


class AmbiguousModelSelectorError(OrchestrationError):
    """Raised when an unqualified alias has more than one enabled match."""


class DisabledProviderError(OrchestrationError):
    """Raised when a selected model's provider is unavailable."""


class UnsupportedCapabilityError(OrchestrationError):
    """Raised when a selected model lacks a required capability."""


class UnsupportedParameterError(OrchestrationError):
    """Raised when a selected model does not expose a supplied canonical option."""


class InvalidParameterValueError(OrchestrationError):
    """Raised when a supplied canonical option violates its declared specification."""


class InvalidExtraParameterError(OrchestrationError):
    """Raised when provider extensions attempt to override Harness-owned fields."""


class InvalidFallbackRouteError(OrchestrationError):
    """Raised when an explicit fallback chain is invalid or duplicates a candidate."""
