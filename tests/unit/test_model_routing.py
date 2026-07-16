"""Deterministic tests for Foundation 2B routing and compatibility policy."""

from __future__ import annotations

import pytest

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
    RouteRequest,
    RouteRequirements,
    SecretRef,
    UnknownModelSelectorError,
    UnsupportedCapabilityError,
    UnsupportedParameterError,
)
from leonervis_code.providers.request_policy import preview_request
from leonervis_code.providers.routing import (
    FAKE_MODEL_CATALOG,
    FAKE_PROVIDER_PROFILES,
    fallback_is_permitted,
    resolve_route,
    retry_is_permitted,
    safe_failure_summary,
)


def test_default_route_resolves_to_the_static_fake_messages_model() -> None:
    route = resolve_route(RouteRequest(primary_selector="default"))

    assert route.primary.provider_id == "fake-messages"
    assert route.primary.model_id == "alpha"
    assert route.primary.canonical_parameters == ()
    assert route.primary.extra_parameters == ()
    assert route.fallbacks == ()


def test_qualified_model_id_with_a_slash_is_preserved() -> None:
    route = resolve_route(RouteRequest(primary_selector="fake-chat/beta/1"))

    assert route.primary.provider_id == "fake-chat"
    assert route.primary.model_id == "beta/1"


def test_ambiguous_alias_requires_a_qualified_selector() -> None:
    catalog = FAKE_MODEL_CATALOG + (
        ModelDefinition(
            provider_id="fake-chat",
            model_id="alpha-copy",
            aliases=("default",),
            capabilities=CapabilitySet(tool_use=True),
            parameters=(),
        ),
    )

    with pytest.raises(AmbiguousModelSelectorError, match="fake-messages/alpha"):
        resolve_route(RouteRequest(primary_selector="default"), catalog=catalog)


def test_unknown_and_disabled_model_selectors_fail_closed() -> None:
    with pytest.raises(UnknownModelSelectorError, match="unknown model selector"):
        resolve_route(RouteRequest(primary_selector="missing"))

    disabled_profiles = (
        ProviderProfile("fake-messages", "fake_messages", enabled=False),
        ProviderProfile("fake-chat", "fake_chat"),
    )
    with pytest.raises(DisabledProviderError, match="disabled"):
        resolve_route(
            RouteRequest(primary_selector="fake-messages/alpha"),
            profiles=disabled_profiles,
        )


def test_route_validates_hard_capabilities_for_primary_and_fallbacks() -> None:
    with pytest.raises(UnsupportedCapabilityError, match="streaming"):
        resolve_route(
            RouteRequest(
                primary_selector="fake-chat/beta/1",
                requirements=RouteRequirements(requires_streaming=True),
            )
        )

    with pytest.raises(UnsupportedCapabilityError, match="streaming"):
        resolve_route(
            RouteRequest(
                primary_selector="default",
                fallback_selectors=("fake-chat/beta/1",),
                requirements=RouteRequirements(requires_streaming=True),
            )
        )


def test_route_rejects_duplicate_fallback_candidates() -> None:
    with pytest.raises(InvalidFallbackRouteError, match="duplicates"):
        resolve_route(
            RouteRequest(
                primary_selector="default",
                fallback_selectors=("fake-messages/alpha",),
            )
        )


def test_route_retains_canonical_parameters_for_the_adapter() -> None:
    route = resolve_route(
        RouteRequest(
            primary_selector="default",
            options=GenerationOptions(max_output_tokens=128, temperature=0.2),
        )
    )

    assert route.primary.canonical_parameters == (
        ("max_output_tokens", 128),
        ("temperature", 0.2),
    )
    assert route.primary.parameter_handling == (
        ("max_output_tokens", ParameterHandling.PASS_TO_ADAPTER),
        ("temperature", ParameterHandling.PASS_TO_ADAPTER),
    )


def test_fake_transformers_not_the_route_resolver_choose_native_parameter_names() -> None:
    options = GenerationOptions(max_output_tokens=128)

    messages_preview = preview_request(
        resolve_route(RouteRequest(primary_selector="default", options=options)).primary
    )
    chat_preview = preview_request(
        resolve_route(RouteRequest(primary_selector="beta", options=options)).primary
    )

    assert messages_preview.native_parameters == (("max_tokens", 128),)
    assert chat_preview.native_parameters == (("max_output_tokens", 128),)


def test_known_fixed_sampling_policy_omits_temperature_with_a_diagnostic() -> None:
    preview = preview_request(
        resolve_route(
            RouteRequest(
                primary_selector="beta",
                options=GenerationOptions(temperature=0.2),
            )
        ).primary
    )

    assert preview.native_parameters == ()
    assert preview.diagnostics[0].code == "temperature_omitted_fixed_sampling"
    assert preview.diagnostics[0].action == "omitted"


def test_unknown_unsupported_parameter_still_fails_closed() -> None:
    unsupported_catalog = (
        ModelDefinition(
            provider_id="fake-messages",
            model_id="alpha",
            aliases=("default",),
            capabilities=CapabilitySet(tool_use=True),
            parameters=(
                ParameterSpec(
                    canonical_name="max_output_tokens",
                    value_kind=ParameterValueKind.INTEGER,
                ),
            ),
        ),
    )
    profiles = (ProviderProfile("fake-messages", "fake_messages"),)

    with pytest.raises(UnsupportedParameterError, match="temperature"):
        resolve_route(
            RouteRequest(
                primary_selector="default",
                options=GenerationOptions(temperature=0.2),
            ),
            catalog=unsupported_catalog,
            profiles=profiles,
        )


def test_catalog_rejects_invalid_parameters() -> None:
    with pytest.raises(InvalidParameterValueError, match="at least 1"):
        resolve_route(
            RouteRequest(
                primary_selector="default",
                options=GenerationOptions(max_output_tokens=0),
            )
        )
    with pytest.raises(InvalidParameterValueError, match="at most 1.0"):
        resolve_route(
            RouteRequest(
                primary_selector="default",
                options=GenerationOptions(temperature=1.1),
            )
        )


def test_allowed_provider_extensions_survive_the_route_and_preview() -> None:
    route = resolve_route(
        RouteRequest(
            primary_selector="default",
            extra_parameters=(("provider_mode", "fast"),),
        )
    )

    assert preview_request(route.primary).extra_parameters == (("provider_mode", "fast"),)


@pytest.mark.parametrize(
    "name",
    ["model", "messages", "stream", "tools", "max_tokens", "temperature"],
)
def test_provider_extensions_cannot_override_harness_or_adapter_fields(name: str) -> None:
    with pytest.raises(InvalidExtraParameterError, match="Harness-owned"):
        resolve_route(
            RouteRequest(
                primary_selector="default",
                extra_parameters=((name, "attempted override"),),
            )
        )


def test_provider_extension_names_must_be_unique_and_nonblank() -> None:
    with pytest.raises(InvalidExtraParameterError, match="duplicated"):
        resolve_route(
            RouteRequest(
                primary_selector="default",
                extra_parameters=(("provider_mode", "one"), ("provider_mode", "two")),
            )
        )
    with pytest.raises(InvalidExtraParameterError, match="must not be blank"):
        resolve_route(RouteRequest(primary_selector="default", extra_parameters=(("", "value"),)))


def test_secret_reference_is_metadata_not_a_resolved_route_value() -> None:
    profiles = {profile.provider_id: profile for profile in FAKE_PROVIDER_PROFILES}
    route = resolve_route(RouteRequest(primary_selector="default"))

    assert profiles[route.primary.provider_id].credential_ref == SecretRef(
        "foundation-2a-fake-messages"
    )
    assert not hasattr(route, "api_key")


@pytest.mark.parametrize(
    "kind",
    [
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.TRANSPORT,
        ProviderFailureKind.PROVIDER_UNAVAILABLE,
    ],
)
def test_transient_failures_are_the_only_future_same_model_retry_candidates(
    kind: ProviderFailureKind,
) -> None:
    assert retry_is_permitted(kind)


@pytest.mark.parametrize(
    "kind",
    [
        ProviderFailureKind.AUTHENTICATION,
        ProviderFailureKind.AUTHORIZATION,
        ProviderFailureKind.INVALID_REQUEST,
        ProviderFailureKind.MODEL_UNAVAILABLE,
        ProviderFailureKind.RESPONSE_INVALID,
        ProviderFailureKind.CONTENT_REFUSAL,
    ],
)
def test_non_transient_failures_are_not_future_same_model_retry_candidates(
    kind: ProviderFailureKind,
) -> None:
    assert not retry_is_permitted(kind)


def test_future_fallback_policy_allows_only_explicit_service_failures() -> None:
    assert fallback_is_permitted(ProviderFailureKind.MODEL_UNAVAILABLE)
    assert fallback_is_permitted(ProviderFailureKind.RATE_LIMITED)
    assert not fallback_is_permitted(ProviderFailureKind.AUTHENTICATION)
    assert not fallback_is_permitted(ProviderFailureKind.INVALID_REQUEST)
    assert not fallback_is_permitted(ProviderFailureKind.CONTENT_REFUSAL)


def test_failure_summary_exposes_only_normalized_safe_fields() -> None:
    failure = ProviderFailure(
        provider_id="fake-messages",
        model_id="alpha",
        kind=ProviderFailureKind.RATE_LIMITED,
        diagnostic_code="rate_limit",
        message="provider temporarily limited this request",
        retryable=True,
        retry_after_seconds=2,
    )

    assert safe_failure_summary(failure) == (
        "provider failure [rate_limited] for fake-messages/alpha: "
        "provider temporarily limited this request"
    )
