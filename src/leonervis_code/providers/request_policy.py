"""Pure fake adapter request-policy previews for Foundation 2B."""

from __future__ import annotations

from collections.abc import Mapping

from leonervis_code.core.orchestration import (
    InvalidExtraParameterError,
    ParameterHandling,
    ProviderDiagnostic,
    ProviderDiagnosticSeverity,
    ProviderRequestPlan,
    ProviderRequestPreview,
    ProviderRequestTransformer,
)


class FakeMessagesRequestTransformer:
    """Preview the fake messages adapter's provider-native option names."""

    def preview(self, plan: ProviderRequestPlan) -> ProviderRequestPreview:
        """Translate accepted canonical options without transport or credentials."""
        return _preview_with_native_names(
            plan,
            native_names={"max_output_tokens": "max_tokens", "temperature": "temperature"},
        )


class FakeChatRequestTransformer:
    """Preview the fake chat adapter's known fixed-sampling policy."""

    def preview(self, plan: ProviderRequestPlan) -> ProviderRequestPreview:
        """Render canonical options and report known fixed-sampling omissions."""
        preview = _preview_with_native_names(
            plan,
            native_names={"max_output_tokens": "max_output_tokens"},
        )
        diagnostics = list(preview.diagnostics)
        for canonical_name, handling in plan.parameter_handling:
            if handling == ParameterHandling.OMIT_WITH_DIAGNOSTIC:
                diagnostics.append(
                    ProviderDiagnostic(
                        code=f"{canonical_name}_omitted_fixed_sampling",
                        severity=ProviderDiagnosticSeverity.INFO,
                        message=(
                            f"{canonical_name} is omitted for known fixed-sampling model "
                            f"{plan.provider_id}/{plan.model_id}"
                        ),
                        action="omitted",
                    )
                )
        return ProviderRequestPreview(
            provider_id=preview.provider_id,
            model_id=preview.model_id,
            native_parameters=preview.native_parameters,
            extra_parameters=preview.extra_parameters,
            diagnostics=tuple(diagnostics),
        )


TRANSFORMERS: Mapping[str, ProviderRequestTransformer] = {
    "fake_messages": FakeMessagesRequestTransformer(),
    "fake_chat": FakeChatRequestTransformer(),
}


def preview_request(plan: ProviderRequestPlan) -> ProviderRequestPreview:
    """Return a pure provider-native preview using the selected adapter key."""
    transformer = TRANSFORMERS.get(plan.adapter_key)
    if transformer is None:
        raise InvalidExtraParameterError(
            f"no request transformer is registered: {plan.adapter_key}"
        )
    return transformer.preview(plan)


def _preview_with_native_names(
    plan: ProviderRequestPlan,
    *,
    native_names: Mapping[str, str],
) -> ProviderRequestPreview:
    native_parameters = tuple(
        (native_names[canonical_name], value) for canonical_name, value in plan.canonical_parameters
    )
    _validate_adapter_owned_fields(plan.extra_parameters, native_parameters)
    return ProviderRequestPreview(
        provider_id=plan.provider_id,
        model_id=plan.model_id,
        native_parameters=native_parameters,
        extra_parameters=plan.extra_parameters,
        diagnostics=(),
    )


def _validate_adapter_owned_fields(
    extra_parameters: tuple[tuple[str, object], ...],
    native_parameters: tuple[tuple[str, int | float], ...],
) -> None:
    generated_names = {name for name, _ in native_parameters}
    for name, _ in extra_parameters:
        if name in generated_names:
            raise InvalidExtraParameterError(
                f"provider extension parameter cannot override adapter-generated field: {name}"
            )
