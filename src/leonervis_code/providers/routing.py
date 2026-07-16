"""Pure offline provider/model routing for Foundation 2B."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from leonervis_code.core.orchestration import (
    AmbiguousModelSelectorError,
    CapabilitySet,
    DisabledProviderError,
    GenerationOptions,
    InvalidExtraParameterError,
    InvalidFallbackRouteError,
    InvalidParameterValueError,
    ModelDefinition,
    ParameterHandling,
    ParameterSpec,
    ParameterValueKind,
    ProviderFailure,
    ProviderFailureKind,
    ProviderProfile,
    ProviderRequestPlan,
    ResolvedRoute,
    RouteRequest,
    SecretRef,
    UnknownModelSelectorError,
    UnsupportedCapabilityError,
    UnsupportedParameterError,
)


PROTECTED_EXTRA_PARAMETER_NAMES = frozenset(
    {
        "model",
        "messages",
        "system",
        "stream",
        "tools",
        "tool_choice",
        "max_output_tokens",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
    }
)

FAKE_PROVIDER_PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        provider_id="fake-messages",
        adapter_key="fake_messages",
        credential_ref=SecretRef("foundation-2a-fake-messages"),
    ),
    ProviderProfile(provider_id="fake-chat", adapter_key="fake_chat"),
)

FAKE_MODEL_CATALOG: tuple[ModelDefinition, ...] = (
    ModelDefinition(
        provider_id="fake-messages",
        model_id="alpha",
        aliases=("default", "alpha"),
        capabilities=CapabilitySet(tool_use=True, streaming=True, system_messages=True),
        parameters=(
            ParameterSpec(
                canonical_name="max_output_tokens",
                value_kind=ParameterValueKind.INTEGER,
                minimum=1,
                maximum=4096,
            ),
            ParameterSpec(
                canonical_name="temperature",
                value_kind=ParameterValueKind.FLOAT,
                minimum=0.0,
                maximum=1.0,
            ),
        ),
    ),
    ModelDefinition(
        provider_id="fake-chat",
        model_id="beta/1",
        aliases=("beta",),
        capabilities=CapabilitySet(tool_use=True, system_messages=True),
        parameters=(
            ParameterSpec(
                canonical_name="max_output_tokens",
                value_kind=ParameterValueKind.INTEGER,
                minimum=1,
                maximum=8192,
            ),
            ParameterSpec(
                canonical_name="temperature",
                value_kind=ParameterValueKind.FLOAT,
                handling=ParameterHandling.OMIT_WITH_DIAGNOSTIC,
                minimum=0.0,
                maximum=1.0,
            ),
        ),
    ),
)

DEFAULT_ROUTE_REQUEST = RouteRequest(primary_selector="default")


def resolve_route(
    request: RouteRequest,
    *,
    catalog: Iterable[ModelDefinition] = FAKE_MODEL_CATALOG,
    profiles: Iterable[ProviderProfile] = FAKE_PROVIDER_PROFILES,
) -> ResolvedRoute:
    """Validate and negotiate an ordered primary-plus-fallback canonical route."""
    _validate_extra_parameters(request.extra_parameters)
    catalog_entries = tuple(catalog)
    profiles_by_id = {profile.provider_id: profile for profile in profiles}
    primary = _compile_candidate(
        request.primary_selector,
        request,
        catalog_entries,
        profiles_by_id,
    )
    seen = {_candidate_key(primary)}
    fallbacks: list[ProviderRequestPlan] = []
    for selector in request.fallback_selectors:
        fallback = _compile_candidate(selector, request, catalog_entries, profiles_by_id)
        key = _candidate_key(fallback)
        if key in seen:
            raise InvalidFallbackRouteError(
                f"fallback route duplicates selected candidate: {fallback.provider_id}/{fallback.model_id}"
            )
        seen.add(key)
        fallbacks.append(fallback)
    return ResolvedRoute(primary=primary, fallbacks=tuple(fallbacks))


def retry_is_permitted(kind: ProviderFailureKind) -> bool:
    """Return whether a future bounded same-model retry could be considered."""
    return kind in {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.TRANSPORT,
        ProviderFailureKind.PROVIDER_UNAVAILABLE,
    }


def fallback_is_permitted(kind: ProviderFailureKind) -> bool:
    """Return whether a future explicit fallback policy could consider failover."""
    return kind in {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.TRANSPORT,
        ProviderFailureKind.PROVIDER_UNAVAILABLE,
        ProviderFailureKind.MODEL_UNAVAILABLE,
    }


def safe_failure_summary(failure: ProviderFailure) -> str:
    """Render stable failure metadata without provider raw payloads or secrets."""
    return (
        f"provider failure [{failure.kind}] for {failure.provider_id}/{failure.model_id}: "
        f"{failure.message}"
    )


def _compile_candidate(
    selector: str,
    request: RouteRequest,
    catalog: tuple[ModelDefinition, ...],
    profiles_by_id: Mapping[str, ProviderProfile],
) -> ProviderRequestPlan:
    model = _resolve_selector(selector, catalog, profiles_by_id)
    profile = profiles_by_id[model.provider_id]
    _validate_capabilities(model, request)
    canonical_parameters, parameter_handling = _negotiate_parameters(model, request.options)
    return ProviderRequestPlan(
        provider_id=model.provider_id,
        adapter_key=profile.adapter_key,
        model_id=model.model_id,
        canonical_parameters=canonical_parameters,
        parameter_handling=parameter_handling,
        extra_parameters=request.extra_parameters,
    )


def _resolve_selector(
    selector: str,
    catalog: tuple[ModelDefinition, ...],
    profiles_by_id: Mapping[str, ProviderProfile],
) -> ModelDefinition:
    if not selector:
        raise UnknownModelSelectorError("model selector must not be blank")
    qualified = _qualified_selector(selector, profiles_by_id)
    if qualified is not None:
        provider_id, model_id = qualified
        matches = tuple(
            model
            for model in catalog
            if model.provider_id == provider_id and model.model_id == model_id
        )
        if not matches:
            raise UnknownModelSelectorError(f"unknown model selector: {selector}")
        return _require_enabled(matches[0], profiles_by_id)

    matches = tuple(
        model for model in catalog if selector == model.model_id or selector in model.aliases
    )
    enabled_matches = tuple(model for model in matches if _is_enabled(model, profiles_by_id))
    if not enabled_matches:
        if matches:
            raise DisabledProviderError(
                f"model selector is configured only for disabled providers: {selector}"
            )
        raise UnknownModelSelectorError(f"unknown model selector: {selector}")
    if len(enabled_matches) > 1:
        choices = ", ".join(f"{model.provider_id}/{model.model_id}" for model in enabled_matches)
        raise AmbiguousModelSelectorError(f"ambiguous model selector {selector}: {choices}")
    return enabled_matches[0]


def _qualified_selector(
    selector: str,
    profiles_by_id: Mapping[str, ProviderProfile],
) -> tuple[str, str] | None:
    provider_id, separator, model_id = selector.partition("/")
    if not separator or provider_id not in profiles_by_id:
        return None
    return provider_id, model_id


def _require_enabled(
    model: ModelDefinition,
    profiles_by_id: Mapping[str, ProviderProfile],
) -> ModelDefinition:
    if not _is_enabled(model, profiles_by_id):
        raise DisabledProviderError(f"provider is disabled: {model.provider_id}")
    return model


def _is_enabled(model: ModelDefinition, profiles_by_id: Mapping[str, ProviderProfile]) -> bool:
    return profiles_by_id.get(model.provider_id, ProviderProfile("", "", enabled=False)).enabled


def _validate_capabilities(model: ModelDefinition, request: RouteRequest) -> None:
    requirements = request.requirements
    missing = tuple(
        name
        for name, required, supported in (
            ("tool_use", requirements.requires_tool_use, model.capabilities.tool_use),
            ("streaming", requirements.requires_streaming, model.capabilities.streaming),
            (
                "system_messages",
                requirements.requires_system_messages,
                model.capabilities.system_messages,
            ),
        )
        if required and not supported
    )
    if missing:
        raise UnsupportedCapabilityError(
            f"model {model.provider_id}/{model.model_id} lacks required capability: {', '.join(missing)}"
        )


def _negotiate_parameters(
    model: ModelDefinition,
    options: GenerationOptions,
) -> tuple[
    tuple[tuple[str, int | float], ...],
    tuple[tuple[str, ParameterHandling], ...],
]:
    supplied_options: tuple[tuple[str, int | float | None], ...] = (
        ("max_output_tokens", options.max_output_tokens),
        ("temperature", options.temperature),
    )
    specs_by_name = {spec.canonical_name: spec for spec in model.parameters}
    canonical_parameters: list[tuple[str, int | float]] = []
    parameter_handling: list[tuple[str, ParameterHandling]] = []
    for canonical_name, value in supplied_options:
        if value is None:
            continue
        spec = specs_by_name.get(canonical_name)
        if spec is None or spec.handling == ParameterHandling.REJECT:
            raise UnsupportedParameterError(
                f"model {model.provider_id}/{model.model_id} does not support {canonical_name}"
            )
        _validate_parameter_value(canonical_name, value, spec)
        parameter_handling.append((canonical_name, spec.handling))
        if spec.handling == ParameterHandling.PASS_TO_ADAPTER:
            canonical_parameters.append((canonical_name, value))
    return tuple(canonical_parameters), tuple(parameter_handling)


def _validate_extra_parameters(extra_parameters: tuple[tuple[str, object], ...]) -> None:
    names: set[str] = set()
    for name, _ in extra_parameters:
        if not name:
            raise InvalidExtraParameterError("provider extension parameter names must not be blank")
        if name in names:
            raise InvalidExtraParameterError(f"provider extension parameter is duplicated: {name}")
        names.add(name)
        if name in PROTECTED_EXTRA_PARAMETER_NAMES:
            raise InvalidExtraParameterError(
                f"provider extension parameter cannot override Harness-owned field: {name}"
            )


def _validate_parameter_value(
    canonical_name: str,
    value: int | float,
    spec: ParameterSpec,
) -> None:
    if spec.value_kind == ParameterValueKind.INTEGER:
        valid_kind = isinstance(value, int) and not isinstance(value, bool)
    else:
        valid_kind = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not valid_kind:
        raise InvalidParameterValueError(f"{canonical_name} has an invalid value type")
    if spec.minimum is not None and value < spec.minimum:
        raise InvalidParameterValueError(f"{canonical_name} must be at least {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise InvalidParameterValueError(f"{canonical_name} must be at most {spec.maximum}")


def _candidate_key(plan: ProviderRequestPlan) -> tuple[str, str]:
    return plan.provider_id, plan.model_id
