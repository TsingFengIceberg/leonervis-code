"""Shared safe failures for real provider adapters."""

from __future__ import annotations

from leonervis_code.core.orchestration import ProviderFailure, ProviderFailureKind


class ProviderAdapterError(RuntimeError):
    """Expose only a normalized safe provider failure to callers."""

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def adapter_error(
    *,
    provider_id: str,
    model_id: str,
    kind: ProviderFailureKind,
    code: str,
    message: str,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
    request_id: str | None = None,
) -> ProviderAdapterError:
    """Build one redacted adapter error from provider-neutral metadata."""
    return ProviderAdapterError(
        ProviderFailure(
            provider_id=provider_id,
            model_id=model_id,
            kind=kind,
            diagnostic_code=code,
            message=message,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
            request_id=request_id,
        )
    )


def safe_request_id(value: object) -> str | None:
    """Retain only short printable provider request identifiers."""
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    return value if value.isprintable() else None


def safe_retry_after(headers: object) -> int | None:
    """Parse a bounded integer Retry-After value from header-like metadata."""
    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get("retry-after")
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    seconds = int(value)
    return seconds if 0 <= seconds <= 86_400 else None
