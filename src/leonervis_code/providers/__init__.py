"""Public provider profile and runtime management APIs."""

from leonervis_code.providers.definitions import (
    ADAPTER_CONTRACT_VERSION,
    route_fingerprint,
)
from leonervis_code.providers.manager import (
    RuntimeProviderManager,
    RuntimeProviderStateError,
    RuntimeStatus,
)
from leonervis_code.providers.model_context import (
    ModelContextCapability,
    ModelContextCapabilityResolver,
    ModelContextSource,
    ModelContextTarget,
)
from leonervis_code.providers.profile import (
    LEGACY_PROFILE_NAMESPACE,
    NamedProviderProfile,
    ProviderProfileError,
    ProviderProfileSpec,
    legacy_profile_id,
    profile_fingerprint,
)
from leonervis_code.providers.profile_store import (
    ActiveProfileSelection,
    ProviderProfileStore,
)
from leonervis_code.providers.request_context import (
    ContextFitDecision,
    ContextFitReport,
    ContextPreflightError,
    ContextPreflightErrorKind,
    RequestTokenCount,
    RequestTokenCountMethod,
    estimate_serialized_input_tokens,
    evaluate_context_fit,
)

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "ActiveProfileSelection",
    "ContextFitDecision",
    "ContextFitReport",
    "ContextPreflightError",
    "ContextPreflightErrorKind",
    "LEGACY_PROFILE_NAMESPACE",
    "ModelContextCapability",
    "ModelContextCapabilityResolver",
    "ModelContextSource",
    "ModelContextTarget",
    "NamedProviderProfile",
    "ProviderProfileError",
    "ProviderProfileSpec",
    "ProviderProfileStore",
    "RequestTokenCount",
    "RequestTokenCountMethod",
    "RuntimeProviderManager",
    "RuntimeProviderStateError",
    "RuntimeStatus",
    "estimate_serialized_input_tokens",
    "evaluate_context_fit",
    "legacy_profile_id",
    "profile_fingerprint",
    "route_fingerprint",
]
