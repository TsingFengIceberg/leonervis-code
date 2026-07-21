"""Provider-neutral request token counting and context-fit contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from typing import Protocol

from leonervis_code.core.contracts import ConversationRequest
from leonervis_code.providers.model_context import ModelContextTarget

MAX_REQUEST_INPUT_TOKENS = 100_000_000


class RequestTokenCountMethod(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ContextFitDecision(StrEnum):
    FITS = "fits"
    CONTEXT_EXCEEDED = "context_exceeded"
    MODEL_OUTPUT_EXCEEDED = "model_output_exceeded"
    UNKNOWN = "unknown"


class ContextPreflightErrorKind(StrEnum):
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    MODEL_OUTPUT_EXCEEDED = "model_output_exceeded"


@dataclass(frozen=True)
class RequestTokenCount:
    input_tokens: int | None
    method: RequestTokenCountMethod
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.method == RequestTokenCountMethod.UNKNOWN:
            if self.input_tokens is not None:
                raise ValueError("unknown token count must not contain input tokens")
            return
        if type(self.input_tokens) is not int or not (
            0 <= self.input_tokens <= MAX_REQUEST_INPUT_TOKENS
        ):
            raise ValueError("known input token count is outside the supported range")

    @classmethod
    def unknown(cls, diagnostic: str | None = None) -> RequestTokenCount:
        return cls(None, RequestTokenCountMethod.UNKNOWN, diagnostic)


class ConversationTokenCounter(Protocol):
    def count_input_tokens(self, request: ConversationRequest) -> RequestTokenCount:
        """Count the adapter-owned native input for one invocation."""


@dataclass(frozen=True)
class ContextFitReport:
    target: ModelContextTarget | None
    input_count: RequestTokenCount
    requested_output_tokens: int
    context_window_limit: int | None
    model_output_limit: int | None
    decision: ContextFitDecision


class ContextPreflightError(Exception):
    """A safe local rejection raised before a conversation request is sent."""

    def __init__(self, kind: ContextPreflightErrorKind, report: ContextFitReport) -> None:
        self.kind = kind
        self.report = report
        super().__init__(_preflight_message(kind, report))


def evaluate_context_fit(
    *,
    target: ModelContextTarget | None,
    input_count: RequestTokenCount,
    requested_output_tokens: int,
    context_window_limit: int | None,
    model_output_limit: int | None,
) -> ContextFitReport:
    if type(requested_output_tokens) is not int or requested_output_tokens < 1:
        raise ValueError("requested output tokens must be a positive integer")
    _validate_optional_limit(context_window_limit, "context window")
    _validate_optional_limit(model_output_limit, "model output")

    if model_output_limit is not None and requested_output_tokens > model_output_limit:
        decision = ContextFitDecision.MODEL_OUTPUT_EXCEEDED
    elif context_window_limit is None or input_count.input_tokens is None:
        decision = ContextFitDecision.UNKNOWN
    elif input_count.input_tokens + requested_output_tokens > context_window_limit:
        decision = ContextFitDecision.CONTEXT_EXCEEDED
    else:
        decision = ContextFitDecision.FITS
    return ContextFitReport(
        target=target,
        input_count=input_count,
        requested_output_tokens=requested_output_tokens,
        context_window_limit=context_window_limit,
        model_output_limit=model_output_limit,
        decision=decision,
    )


def estimate_serialized_input_tokens(value: object) -> RequestTokenCount:
    """Estimate compact UTF-8 JSON input using ceil(serialized bytes / 4)."""
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("native input projection is not JSON serializable") from error
    estimate = math.ceil(len(payload) / 4)
    if estimate > MAX_REQUEST_INPUT_TOKENS:
        raise ValueError("estimated input token count exceeds the supported range")
    return RequestTokenCount(estimate, RequestTokenCountMethod.ESTIMATED)


def raise_for_context_fit(report: ContextFitReport) -> None:
    if report.decision == ContextFitDecision.MODEL_OUTPUT_EXCEEDED:
        raise ContextPreflightError(ContextPreflightErrorKind.MODEL_OUTPUT_EXCEEDED, report)
    if report.decision == ContextFitDecision.CONTEXT_EXCEEDED:
        raise ContextPreflightError(ContextPreflightErrorKind.CONTEXT_WINDOW_EXCEEDED, report)


def _validate_optional_limit(value: int | None, label: str) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f"{label} limit must be a positive integer or unknown")


def _preflight_message(kind: ContextPreflightErrorKind, report: ContextFitReport) -> str:
    if kind == ContextPreflightErrorKind.MODEL_OUTPUT_EXCEEDED:
        return (
            f"context preflight rejected request: output reserve="
            f"{report.requested_output_tokens} > model max output="
            f"{report.model_output_limit}"
        )
    return (
        f"context preflight rejected request: input={report.input_count.input_tokens} "
        f"({report.input_count.method.value}) + output reserve="
        f"{report.requested_output_tokens} > context window="
        f"{report.context_window_limit}"
    )
