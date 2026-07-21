from __future__ import annotations

import pytest

from leonervis_code.providers.request_context import (
    ContextFitDecision,
    ContextPreflightError,
    ContextPreflightErrorKind,
    RequestTokenCount,
    RequestTokenCountMethod,
    estimate_serialized_input_tokens,
    evaluate_context_fit,
    raise_for_context_fit,
)


def count(tokens: int, method=RequestTokenCountMethod.EXACT) -> RequestTokenCount:
    return RequestTokenCount(tokens, method)


def test_exact_context_boundary_fits_and_plus_one_is_rejected() -> None:
    boundary = evaluate_context_fit(
        target=None,
        input_count=count(900),
        requested_output_tokens=100,
        context_window_limit=1_000,
        model_output_limit=200,
    )
    assert boundary.decision == ContextFitDecision.FITS

    exceeded = evaluate_context_fit(
        target=None,
        input_count=count(901),
        requested_output_tokens=100,
        context_window_limit=1_000,
        model_output_limit=200,
    )
    assert exceeded.decision == ContextFitDecision.CONTEXT_EXCEEDED
    with pytest.raises(ContextPreflightError) as raised:
        raise_for_context_fit(exceeded)
    assert raised.value.kind == ContextPreflightErrorKind.CONTEXT_WINDOW_EXCEEDED
    assert "input=901 (exact) + output reserve=100 > context window=1000" in str(raised.value)


def test_model_output_limit_is_checked_without_an_input_count() -> None:
    report = evaluate_context_fit(
        target=None,
        input_count=RequestTokenCount.unknown("no counter"),
        requested_output_tokens=101,
        context_window_limit=None,
        model_output_limit=100,
    )
    assert report.decision == ContextFitDecision.MODEL_OUTPUT_EXCEEDED
    with pytest.raises(ContextPreflightError) as raised:
        raise_for_context_fit(report)
    assert raised.value.kind == ContextPreflightErrorKind.MODEL_OUTPUT_EXCEEDED


def test_unknown_facts_fail_open_and_preserve_count_attribution() -> None:
    unknown_limit = evaluate_context_fit(
        target=None,
        input_count=count(10, RequestTokenCountMethod.ESTIMATED),
        requested_output_tokens=20,
        context_window_limit=None,
        model_output_limit=None,
    )
    assert unknown_limit.decision == ContextFitDecision.UNKNOWN
    assert unknown_limit.input_count.method == RequestTokenCountMethod.ESTIMATED

    unknown_count = evaluate_context_fit(
        target=None,
        input_count=RequestTokenCount.unknown("unsupported"),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=None,
    )
    assert unknown_count.decision == ContextFitDecision.UNKNOWN
    raise_for_context_fit(unknown_count)


def test_serialized_estimator_is_compact_utf8_and_deterministic() -> None:
    first = estimate_serialized_input_tokens({"b": "狮子", "a": [1, True]})
    second = estimate_serialized_input_tokens({"a": [1, True], "b": "狮子"})
    assert first == second
    assert first.method == RequestTokenCountMethod.ESTIMATED
    assert first.input_tokens == 7


def test_token_count_and_fit_inputs_are_strictly_validated() -> None:
    with pytest.raises(ValueError):
        RequestTokenCount(True, RequestTokenCountMethod.EXACT)
    with pytest.raises(ValueError):
        RequestTokenCount(1, RequestTokenCountMethod.UNKNOWN)
    with pytest.raises(ValueError):
        evaluate_context_fit(
            target=None,
            input_count=count(1),
            requested_output_tokens=0,
            context_window_limit=10,
            model_output_limit=None,
        )
    with pytest.raises(ValueError):
        estimate_serialized_input_tokens({"bad": float("nan")})
