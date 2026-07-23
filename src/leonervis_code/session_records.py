"""Closed versioned records for durable Leonervis Code sessions.

This module owns only the typed transcript format and replay invariants. Filesystem
safety, locking, and durability live in :mod:`leonervis_code.session_store`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import TypeAlias
from urllib.parse import urlparse
from uuid import UUID

from leonervis_code.core.compaction import (
    COMPACT_MIN_EFFECTIVE_TURNS,
    COMPACT_PROMPT_VERSION,
    COMPACT_RETAINED_TURNS,
    SUMMARY_CONTINUATION_VERSION,
    CompactionTrigger,
    EffectiveContextSummary,
    build_compact_prompt,
    summary_continuation_fingerprint,
)
from leonervis_code.core.contracts import (
    AssistantText,
    ConversationItem,
    ConversationTurn,
    ToolArguments,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.core.effective_context import (
    COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT,
    EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
    validate_complete_history,
)

SCHEMA_VERSION = 1
TURN_COMMITTED_LEGACY_SCHEMA_VERSION = 1
TURN_COMMITTED_SCHEMA_VERSION = 2
CONTEXT_COMPACTED_LEGACY_SCHEMA_VERSION = 2
CONTEXT_COMPACTED_SCHEMA_VERSION = 3
WORKSPACE_FINGERPRINT_VERSION = "v1"
MAX_RECORD_BYTES = 1024 * 1024
MAX_RECORDS = 100_000
MAX_TEXT_BYTES = 512 * 1024
MAX_STRING_LENGTH = 4096

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WORKSPACE_FINGERPRINT = re.compile(r"v1-[0-9a-f]{64}\Z")


class SessionRecordError(ValueError):
    """Raised when a session record or replay chain is invalid."""


@dataclass(frozen=True)
class BindingSnapshot:
    """Redacted, immutable provider-route binding captured with durable events."""

    profile_id: str | None
    profile_revision: int | None
    profile_name: str | None
    profile_fingerprint: str | None
    provider_id: str
    protocol: str | None
    selected_model: str | None
    wire_model: str | None
    base_url: str | None
    base_url_source: str | None
    source: str
    credential_env: str | None
    max_output_tokens: int | None
    temperature: float | None
    generation: int
    adapter_version: str
    route_fingerprint: str

    def __post_init__(self) -> None:
        _optional_text(self.profile_id, "binding profile_id")
        if self.profile_revision is not None and (
            type(self.profile_revision) is not int or self.profile_revision < 0
        ):
            raise SessionRecordError(
                "binding profile_revision must be a non-negative integer or null"
            )
        _optional_text(self.profile_name, "binding profile_name")
        _optional_sha256(self.profile_fingerprint, "binding profile_fingerprint")
        _required_text(self.provider_id, "binding provider_id")
        _optional_text(self.protocol, "binding protocol")
        _optional_text(self.selected_model, "binding selected_model")
        _optional_text(self.wire_model, "binding wire_model")
        _validate_base_url(self.base_url)
        _optional_text(self.base_url_source, "binding base_url_source")
        _required_text(self.source, "binding source")
        if self.credential_env is not None:
            if (
                not isinstance(self.credential_env, str)
                or _ENVIRONMENT_NAME.fullmatch(self.credential_env) is None
            ):
                raise SessionRecordError(
                    "binding credential_env must be a portable environment name or null"
                )
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens < 1
        ):
            raise SessionRecordError("binding max_output_tokens must be a positive integer or null")
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0.0 <= float(self.temperature) <= 2.0
        ):
            raise SessionRecordError("binding temperature must be between 0.0 and 2.0 or null")
        if type(self.generation) is not int or self.generation < 0:
            raise SessionRecordError("binding generation must be a non-negative integer")
        _required_text(self.adapter_version, "binding adapter_version")
        _required_sha256(self.route_fingerprint, "binding route_fingerprint")

    @classmethod
    def fake(
        cls,
        *,
        generation: int = 0,
        adapter_version: str = "fake-v1",
        source: str = "default",
    ) -> BindingSnapshot:
        """Build a complete redacted snapshot for the deterministic fake runtime."""
        fingerprint = hashlib.sha256(
            f"fake\0{adapter_version}\0{source}".encode("utf-8")
        ).hexdigest()
        return cls(
            profile_id=None,
            profile_revision=None,
            profile_name=None,
            profile_fingerprint=None,
            provider_id="fake",
            protocol=None,
            selected_model=None,
            wire_model=None,
            base_url=None,
            base_url_source=None,
            source=source,
            credential_env=None,
            max_output_tokens=None,
            temperature=None,
            generation=generation,
            adapter_version=adapter_version,
            route_fingerprint=fingerprint,
        )


@dataclass(frozen=True)
class SessionHeader:
    sequence: int
    session_id: str
    workspace: str
    workspace_fingerprint: str
    created_at: str
    binding: BindingSnapshot
    record_type: str = "session_header"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class TurnCommitted:
    sequence: int
    committed_at: str
    binding: BindingSnapshot
    items: tuple[ConversationItem, ...]
    record_type: str = "turn_committed"
    schema_version: int = TURN_COMMITTED_SCHEMA_VERSION


@dataclass(frozen=True)
class RuntimeChanged:
    sequence: int
    occurred_at: str
    binding: BindingSnapshot
    reason: str
    record_type: str = "runtime_changed"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class TurnFailed:
    sequence: int
    occurred_at: str
    binding: BindingSnapshot
    failure_kind: str
    message: str
    record_type: str = "turn_failed"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class SessionResumed:
    sequence: int
    occurred_at: str
    record_type: str = "session_resumed"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class Recovery:
    sequence: int
    occurred_at: str
    truncated_bytes: int
    record_type: str = "recovery"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class ContextCompacted:
    sequence: int
    occurred_at: str
    binding: BindingSnapshot
    source_context_id: str
    result_context_id: str
    source_full_turn_count: int
    source_effective_turn_count: int
    retained_from_full_turn: int
    previous_checkpoint_sequence: int | None
    summary: str
    compact_prompt_version: int
    compact_prompt_fingerprint: str
    continuation_version: int
    continuation_fingerprint: str
    effective_context_representation_version: int
    trigger: CompactionTrigger = CompactionTrigger.MANUAL
    high_water_percent: int | None = None
    record_type: str = "context_compacted"
    schema_version: int = CONTEXT_COMPACTED_SCHEMA_VERSION


@dataclass(frozen=True)
class SessionClosed:
    sequence: int
    occurred_at: str
    reason: str
    record_type: str = "session_closed"
    schema_version: int = SCHEMA_VERSION


SessionRecord: TypeAlias = (
    SessionHeader
    | TurnCommitted
    | RuntimeChanged
    | TurnFailed
    | SessionResumed
    | Recovery
    | ContextCompacted
    | SessionClosed
)
AuditRecord: TypeAlias = RuntimeChanged | TurnFailed | SessionResumed | Recovery | SessionClosed


@dataclass(frozen=True)
class ReplayState:
    """Validated transcript state; audit records are intentionally absent from history."""

    header: SessionHeader
    records: tuple[SessionRecord, ...]
    history: tuple[ConversationItem, ...]
    effective_history: tuple[ConversationItem, ...]
    effective_summary: EffectiveContextSummary | None
    effective_source: str
    latest_checkpoint: ContextCompacted | None
    turns: tuple[ConversationTurn, ...]
    binding: BindingSnapshot
    next_sequence: int
    closed: bool


def canonical_session_id(value: object) -> str:
    """Return a canonical lowercase UUID string or fail closed."""
    if not isinstance(value, str):
        raise SessionRecordError("session ID must be text")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise SessionRecordError("session ID must be a canonical UUID") from None
    if parsed.version != 4 or str(parsed) != value:
        raise SessionRecordError("session ID must be a canonical UUID4")
    return value


def workspace_fingerprint(workspace: Path) -> str:
    """Hash one canonical workspace identity using a domain-separated v1 SHA-256."""
    canonical = os.fsencode(str(Path(workspace).resolve(strict=True)))
    digest = hashlib.sha256(b"leonervis-code-workspace-v1\0" + canonical).hexdigest()
    return f"{WORKSPACE_FINGERPRINT_VERSION}-{digest}"


def encode_record(record: SessionRecord) -> bytes:
    """Encode one record as a compact canonical JSONL line."""
    data = _record_to_dict(record)
    try:
        payload = (
            json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        raise SessionRecordError("session record is not JSON encodable") from None
    if len(payload) > MAX_RECORD_BYTES:
        raise SessionRecordError(f"session record exceeds {MAX_RECORD_BYTES} bytes")
    return payload


def decode_record(payload: bytes) -> SessionRecord:
    """Decode one complete JSON record and reject unknown fields or types."""
    if not isinstance(payload, bytes):
        raise SessionRecordError("session record payload must be bytes")
    if not payload or len(payload) > MAX_RECORD_BYTES:
        raise SessionRecordError("session record is empty or oversized")
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    if not payload or b"\n" in payload or b"\r" in payload:
        raise SessionRecordError("session record must occupy exactly one JSONL line")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise SessionRecordError("session record is not valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise SessionRecordError("session record must be a JSON object")
    return _record_from_dict(value)


def replay_records(
    records: tuple[SessionRecord, ...] | list[SessionRecord],
    *,
    expected_workspace: str | None = None,
    expected_workspace_fingerprint: str | None = None,
    expected_session_id: str | None = None,
    expected_file_name: str | None = None,
) -> ReplayState:
    """Validate sequence, binding, closed turns, and tool-use causality."""
    if not records:
        raise SessionRecordError("session transcript is missing its header")
    if len(records) > MAX_RECORDS:
        raise SessionRecordError(f"session transcript exceeds {MAX_RECORDS} records")
    header = records[0]
    if not isinstance(header, SessionHeader):
        raise SessionRecordError("session_header must be the first record")
    _validate_header(header)
    if expected_workspace is not None and header.workspace != expected_workspace:
        raise SessionRecordError("session workspace does not match the current workspace")
    if (
        expected_workspace_fingerprint is not None
        and header.workspace_fingerprint != expected_workspace_fingerprint
    ):
        raise SessionRecordError("session workspace fingerprint does not match")
    if expected_session_id is not None and header.session_id != expected_session_id:
        raise SessionRecordError("session ID does not match the selected transcript")
    if expected_file_name is not None and expected_file_name != f"{header.session_id}.jsonl":
        raise SessionRecordError("session transcript file name does not match its header")

    history: list[ConversationItem] = []
    effective_history: list[ConversationItem] = []
    effective_summary: EffectiveContextSummary | None = None
    effective_source = EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY
    latest_checkpoint: ContextCompacted | None = None
    turns: list[ConversationTurn] = []
    binding = header.binding
    closed = False
    seen_tool_ids: set[str] = set()
    validated: list[SessionRecord] = []
    for expected_sequence, record in enumerate(records):
        if record.sequence != expected_sequence:
            raise SessionRecordError(
                f"session sequence mismatch: expected {expected_sequence}, got {record.sequence}"
            )
        _validate_record_version(record)
        if expected_sequence and isinstance(record, SessionHeader):
            raise SessionRecordError("session_header may only be the first record")
        if closed and not isinstance(record, (Recovery, SessionResumed)):
            raise SessionRecordError(
                "session transcript requires session_resumed after session_closed"
            )
        if isinstance(record, TurnCommitted):
            _validate_timestamp(record.committed_at, "turn committed_at")
            _validate_turn(record.items, seen_tool_ids)
            history.extend(record.items)
            effective_history.extend(record.items)
            turns.append(ConversationTurn(user=record.items[0], assistant=record.items[-1]))  # type: ignore[arg-type]
            binding = record.binding
        elif isinstance(record, RuntimeChanged):
            _validate_timestamp(record.occurred_at, "runtime_changed occurred_at")
            _required_text(record.reason, "runtime_changed reason", allow_empty=True)
            binding = record.binding
        elif isinstance(record, TurnFailed):
            _validate_timestamp(record.occurred_at, "turn_failed occurred_at")
            _required_text(record.failure_kind, "turn_failed failure_kind")
            _required_text(record.message, "turn_failed message", allow_empty=True)
            binding = record.binding
        elif isinstance(record, SessionResumed):
            _validate_timestamp(record.occurred_at, "session_resumed occurred_at")
            closed = False
        elif isinstance(record, Recovery):
            _validate_timestamp(record.occurred_at, "recovery occurred_at")
            if type(record.truncated_bytes) is not int or record.truncated_bytes < 1:
                raise SessionRecordError("recovery truncated_bytes must be a positive integer")
        elif isinstance(record, ContextCompacted):
            _validate_context_compacted(
                record,
                full_history=tuple(history),
                effective_history=tuple(effective_history),
                latest_checkpoint=latest_checkpoint,
            )
            full_turns = validate_complete_history(tuple(history)).complete_turns
            retained_turns = full_turns[record.retained_from_full_turn :]
            effective_history = [item for turn in retained_turns for item in turn.items]
            effective_summary = EffectiveContextSummary(
                record.summary,
                continuation_version=record.continuation_version,
                continuation_fingerprint=record.continuation_fingerprint,
            )
            effective_source = EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT
            latest_checkpoint = record
            binding = record.binding
        elif isinstance(record, SessionClosed):
            _validate_timestamp(record.occurred_at, "session_closed occurred_at")
            _required_text(record.reason, "session_closed reason", allow_empty=True)
            closed = True
        elif not isinstance(record, SessionHeader):
            raise SessionRecordError("unsupported session record")
        validated.append(record)
    return ReplayState(
        header=header,
        records=tuple(validated),
        history=tuple(history),
        effective_history=tuple(effective_history),
        effective_summary=effective_summary,
        effective_source=effective_source,
        latest_checkpoint=latest_checkpoint,
        turns=tuple(turns),
        binding=binding,
        next_sequence=len(validated),
        closed=closed,
    )


def _record_to_dict(record: SessionRecord) -> dict[str, object]:
    _validate_record_version(record)
    common: dict[str, object] = {
        "record_type": record.record_type,
        "schema_version": record.schema_version,
        "sequence": record.sequence,
    }
    if isinstance(record, SessionHeader):
        _validate_header(record)
        common.update(
            session_id=record.session_id,
            workspace=record.workspace,
            workspace_fingerprint=record.workspace_fingerprint,
            created_at=record.created_at,
            binding=_binding_to_dict(record.binding),
        )
    elif isinstance(record, TurnCommitted):
        _validate_timestamp(record.committed_at, "turn committed_at")
        _validate_turn(record.items, set())
        common.update(
            committed_at=record.committed_at,
            binding=_binding_to_dict(record.binding),
            items=[
                _item_to_dict(item, schema_version=record.schema_version) for item in record.items
            ],
        )
    elif isinstance(record, RuntimeChanged):
        _validate_timestamp(record.occurred_at, "runtime_changed occurred_at")
        _required_text(record.reason, "runtime_changed reason", allow_empty=True)
        common.update(
            occurred_at=record.occurred_at,
            binding=_binding_to_dict(record.binding),
            reason=record.reason,
        )
    elif isinstance(record, TurnFailed):
        _validate_timestamp(record.occurred_at, "turn_failed occurred_at")
        _required_text(record.failure_kind, "turn_failed failure_kind")
        _required_text(record.message, "turn_failed message", allow_empty=True)
        common.update(
            occurred_at=record.occurred_at,
            binding=_binding_to_dict(record.binding),
            failure_kind=record.failure_kind,
            message=record.message,
        )
    elif isinstance(record, SessionResumed):
        _validate_timestamp(record.occurred_at, "session_resumed occurred_at")
        common["occurred_at"] = record.occurred_at
    elif isinstance(record, Recovery):
        _validate_timestamp(record.occurred_at, "recovery occurred_at")
        if type(record.truncated_bytes) is not int or record.truncated_bytes < 1:
            raise SessionRecordError("recovery truncated_bytes must be a positive integer")
        common.update(occurred_at=record.occurred_at, truncated_bytes=record.truncated_bytes)
    elif isinstance(record, ContextCompacted):
        _validate_context_compacted_fields(record)
        common.update(
            occurred_at=record.occurred_at,
            binding=_binding_to_dict(record.binding),
            source_context_id=record.source_context_id,
            result_context_id=record.result_context_id,
            source_full_turn_count=record.source_full_turn_count,
            source_effective_turn_count=record.source_effective_turn_count,
            retained_from_full_turn=record.retained_from_full_turn,
            previous_checkpoint_sequence=record.previous_checkpoint_sequence,
            summary=record.summary,
            compact_prompt_version=record.compact_prompt_version,
            compact_prompt_fingerprint=record.compact_prompt_fingerprint,
            continuation_version=record.continuation_version,
            continuation_fingerprint=record.continuation_fingerprint,
            effective_context_representation_version=record.effective_context_representation_version,
        )
        if record.schema_version == CONTEXT_COMPACTED_SCHEMA_VERSION:
            common.update(
                trigger=record.trigger.value,
                high_water_percent=record.high_water_percent,
            )
    elif isinstance(record, SessionClosed):
        _validate_timestamp(record.occurred_at, "session_closed occurred_at")
        _required_text(record.reason, "session_closed reason", allow_empty=True)
        common.update(occurred_at=record.occurred_at, reason=record.reason)
    else:
        raise SessionRecordError("unsupported session record")
    return common


def _record_from_dict(value: dict[str, object]) -> SessionRecord:
    record_type = _required_field_text(value, "record_type", "session record")
    version = value.get("schema_version")
    if record_type == "context_compacted":
        allowed_versions = {
            CONTEXT_COMPACTED_LEGACY_SCHEMA_VERSION,
            CONTEXT_COMPACTED_SCHEMA_VERSION,
        }
    elif record_type == "turn_committed":
        allowed_versions = {
            TURN_COMMITTED_LEGACY_SCHEMA_VERSION,
            TURN_COMMITTED_SCHEMA_VERSION,
        }
    else:
        allowed_versions = {SCHEMA_VERSION}
    if type(version) is not int or version not in allowed_versions:
        raise SessionRecordError("unsupported session record schema version")
    sequence = value.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise SessionRecordError("session record sequence must be a non-negative integer")

    if record_type == "session_header":
        _closed_fields(
            value,
            {
                "record_type",
                "schema_version",
                "sequence",
                "session_id",
                "workspace",
                "workspace_fingerprint",
                "created_at",
                "binding",
            },
            record_type,
        )
        record = SessionHeader(
            sequence=sequence,
            session_id=_required_field_text(value, "session_id", record_type),
            workspace=_required_field_text(value, "workspace", record_type),
            workspace_fingerprint=_required_field_text(value, "workspace_fingerprint", record_type),
            created_at=_required_field_text(value, "created_at", record_type),
            binding=_binding_from_value(value.get("binding")),
        )
        _validate_header(record)
        return record
    if record_type == "turn_committed":
        _closed_fields(
            value,
            {
                "record_type",
                "schema_version",
                "sequence",
                "committed_at",
                "binding",
                "items",
            },
            record_type,
        )
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise SessionRecordError("turn_committed items must be an array")
        items = tuple(_item_from_value(item, schema_version=version) for item in raw_items)
        record = TurnCommitted(
            sequence=sequence,
            committed_at=_required_field_text(value, "committed_at", record_type),
            binding=_binding_from_value(value.get("binding")),
            items=items,
            schema_version=version,
        )
        _validate_timestamp(record.committed_at, "turn committed_at")
        _validate_turn(record.items, set())
        return record
    if record_type == "runtime_changed":
        fields = {
            "record_type",
            "schema_version",
            "sequence",
            "occurred_at",
            "binding",
            "reason",
        }
        _closed_fields(value, fields, record_type)
        return RuntimeChanged(
            sequence=sequence,
            occurred_at=_required_field_text(value, "occurred_at", record_type),
            binding=_binding_from_value(value.get("binding")),
            reason=_required_field_text(value, "reason", record_type, allow_empty=True),
        )
    if record_type == "turn_failed":
        fields = {
            "record_type",
            "schema_version",
            "sequence",
            "occurred_at",
            "binding",
            "failure_kind",
            "message",
        }
        _closed_fields(value, fields, record_type)
        return TurnFailed(
            sequence=sequence,
            occurred_at=_required_field_text(value, "occurred_at", record_type),
            binding=_binding_from_value(value.get("binding")),
            failure_kind=_required_field_text(value, "failure_kind", record_type),
            message=_required_field_text(value, "message", record_type, allow_empty=True),
        )
    if record_type == "context_compacted":
        fields = {
            "record_type",
            "schema_version",
            "sequence",
            "occurred_at",
            "binding",
            "source_context_id",
            "result_context_id",
            "source_full_turn_count",
            "source_effective_turn_count",
            "retained_from_full_turn",
            "previous_checkpoint_sequence",
            "summary",
            "compact_prompt_version",
            "compact_prompt_fingerprint",
            "continuation_version",
            "continuation_fingerprint",
            "effective_context_representation_version",
        }
        if version == CONTEXT_COMPACTED_SCHEMA_VERSION:
            fields |= {"trigger", "high_water_percent"}
        _closed_fields(value, fields, record_type)
        previous = value.get("previous_checkpoint_sequence")
        if previous is not None and (type(previous) is not int or previous < 0):
            raise SessionRecordError(
                "context_compacted previous_checkpoint_sequence must be non-negative or null"
            )
        if version == CONTEXT_COMPACTED_SCHEMA_VERSION:
            try:
                trigger = CompactionTrigger(_required_field_text(value, "trigger", record_type))
            except ValueError:
                raise SessionRecordError("context_compacted trigger is invalid") from None
            high_water_percent = _nullable_field_int(value, "high_water_percent", record_type)
        else:
            trigger = CompactionTrigger.MANUAL
            high_water_percent = None
        record = ContextCompacted(
            sequence=sequence,
            occurred_at=_required_field_text(value, "occurred_at", record_type),
            binding=_binding_from_value(value.get("binding")),
            source_context_id=_required_field_text(value, "source_context_id", record_type),
            result_context_id=_required_field_text(value, "result_context_id", record_type),
            source_full_turn_count=_required_field_int(
                value, "source_full_turn_count", record_type
            ),
            source_effective_turn_count=_required_field_int(
                value, "source_effective_turn_count", record_type
            ),
            retained_from_full_turn=_required_field_int(
                value, "retained_from_full_turn", record_type
            ),
            previous_checkpoint_sequence=previous,
            summary=_required_field_text(value, "summary", record_type),
            compact_prompt_version=_required_field_int(
                value, "compact_prompt_version", record_type
            ),
            compact_prompt_fingerprint=_required_field_text(
                value, "compact_prompt_fingerprint", record_type
            ),
            continuation_version=_required_field_int(value, "continuation_version", record_type),
            continuation_fingerprint=_required_field_text(
                value, "continuation_fingerprint", record_type
            ),
            effective_context_representation_version=_required_field_int(
                value, "effective_context_representation_version", record_type
            ),
            trigger=trigger,
            high_water_percent=high_water_percent,
            schema_version=version,
        )
        _validate_context_compacted_fields(record)
        return record
    simple_fields = {"record_type", "schema_version", "sequence", "occurred_at"}
    if record_type == "session_resumed":
        _closed_fields(value, simple_fields, record_type)
        return SessionResumed(
            sequence=sequence,
            occurred_at=_required_field_text(value, "occurred_at", record_type),
        )
    if record_type == "recovery":
        _closed_fields(value, simple_fields | {"truncated_bytes"}, record_type)
        truncated = value.get("truncated_bytes")
        if type(truncated) is not int or truncated < 1:
            raise SessionRecordError("recovery truncated_bytes must be a positive integer")
        return Recovery(
            sequence=sequence,
            occurred_at=_required_field_text(value, "occurred_at", record_type),
            truncated_bytes=truncated,
        )
    if record_type == "session_closed":
        _closed_fields(value, simple_fields | {"reason"}, record_type)
        return SessionClosed(
            sequence=sequence,
            occurred_at=_required_field_text(value, "occurred_at", record_type),
            reason=_required_field_text(value, "reason", record_type, allow_empty=True),
        )
    raise SessionRecordError(f"unknown session record type: {record_type}")


def _binding_to_dict(binding: BindingSnapshot) -> dict[str, object]:
    binding.__post_init__()
    return {
        "profile_id": binding.profile_id,
        "profile_revision": binding.profile_revision,
        "profile_name": binding.profile_name,
        "profile_fingerprint": binding.profile_fingerprint,
        "provider_id": binding.provider_id,
        "protocol": binding.protocol,
        "selected_model": binding.selected_model,
        "wire_model": binding.wire_model,
        "base_url": binding.base_url,
        "base_url_source": binding.base_url_source,
        "source": binding.source,
        "credential_env": binding.credential_env,
        "max_output_tokens": binding.max_output_tokens,
        "temperature": binding.temperature,
        "generation": binding.generation,
        "adapter_version": binding.adapter_version,
        "route_fingerprint": binding.route_fingerprint,
    }


def _binding_from_value(value: object) -> BindingSnapshot:
    if not isinstance(value, dict):
        raise SessionRecordError("binding must be a JSON object")
    fields = {
        "profile_id",
        "profile_revision",
        "profile_name",
        "profile_fingerprint",
        "provider_id",
        "protocol",
        "selected_model",
        "wire_model",
        "base_url",
        "base_url_source",
        "source",
        "credential_env",
        "max_output_tokens",
        "temperature",
        "generation",
        "adapter_version",
        "route_fingerprint",
    }
    _closed_fields(value, fields, "binding")
    return BindingSnapshot(
        profile_id=_nullable_field_text(value, "profile_id", "binding"),
        profile_revision=_nullable_field_int(value, "profile_revision", "binding"),
        profile_name=_nullable_field_text(value, "profile_name", "binding"),
        profile_fingerprint=_nullable_field_text(value, "profile_fingerprint", "binding"),
        provider_id=_required_field_text(value, "provider_id", "binding"),
        protocol=_nullable_field_text(value, "protocol", "binding"),
        selected_model=_nullable_field_text(value, "selected_model", "binding"),
        wire_model=_nullable_field_text(value, "wire_model", "binding"),
        base_url=_nullable_field_text(value, "base_url", "binding"),
        base_url_source=_nullable_field_text(value, "base_url_source", "binding"),
        source=_required_field_text(value, "source", "binding"),
        credential_env=_nullable_field_text(value, "credential_env", "binding"),
        max_output_tokens=_nullable_field_int(value, "max_output_tokens", "binding"),
        temperature=_nullable_field_number(value, "temperature", "binding"),
        generation=_required_field_int(value, "generation", "binding"),
        adapter_version=_required_field_text(value, "adapter_version", "binding"),
        route_fingerprint=_required_field_text(value, "route_fingerprint", "binding"),
    )


def _item_to_dict(
    item: ConversationItem,
    *,
    schema_version: int = TURN_COMMITTED_SCHEMA_VERSION,
) -> dict[str, object]:
    if schema_version not in {
        TURN_COMMITTED_LEGACY_SCHEMA_VERSION,
        TURN_COMMITTED_SCHEMA_VERSION,
    }:
        raise SessionRecordError("unsupported turn_committed schema version")
    if isinstance(item, UserMessage):
        _text_payload(item.text, "user message text")
        return {"item_type": "user_message", "text": item.text}
    if isinstance(item, AssistantText):
        _text_payload(item.text, "assistant text")
        return {"item_type": "assistant_text", "text": item.text}
    if isinstance(item, ToolUse):
        _required_text(item.tool_use_id, "tool_use ID")
        _required_text(item.name, "tool_use name")
        if not isinstance(item.arguments, ToolArguments):
            raise SessionRecordError("tool_use arguments are invalid")
        arguments = item.arguments.as_mapping()
        if schema_version == TURN_COMMITTED_LEGACY_SCHEMA_VERSION:
            if item.name == "read_file" and set(arguments) == {"path"}:
                path = arguments["path"]
            elif item.name == "glob" and set(arguments) == {"pattern"}:
                path = arguments["pattern"]
            elif item.name not in {"read_file", "glob", "grep"} and set(arguments) == {"path"}:
                path = arguments["path"]
            else:
                raise SessionRecordError("tool_use cannot be represented by schema version 1")
            _required_text(path, "tool_use path")
            return {
                "item_type": "tool_use",
                "tool_use_id": item.tool_use_id,
                "name": item.name,
                "path": path,
            }
        return {
            "item_type": "tool_use",
            "tool_use_id": item.tool_use_id,
            "name": item.name,
            "arguments_version": item.arguments.version,
            "arguments": arguments,
        }
    if isinstance(item, ToolResult):
        _required_text(item.tool_use_id, "tool_result ID")
        _text_payload(item.content, "tool_result content")
        if type(item.is_error) is not bool or type(item.truncated) is not bool:
            raise SessionRecordError("tool_result flags must be booleans")
        return {
            "item_type": "tool_result",
            "tool_use_id": item.tool_use_id,
            "content": item.content,
            "is_error": item.is_error,
            "truncated": item.truncated,
        }
    raise SessionRecordError("turn contains an unsupported conversation item")


def _item_from_value(value: object, *, schema_version: int) -> ConversationItem:
    if not isinstance(value, dict):
        raise SessionRecordError("turn item must be a JSON object")
    item_type = _required_field_text(value, "item_type", "turn item")
    if item_type in {"user_message", "assistant_text"}:
        _closed_fields(value, {"item_type", "text"}, item_type)
        text = _required_field_payload_text(value, "text", item_type)
        return UserMessage(text) if item_type == "user_message" else AssistantText(text)
    if item_type == "tool_use":
        if schema_version == TURN_COMMITTED_LEGACY_SCHEMA_VERSION:
            _closed_fields(value, {"item_type", "tool_use_id", "name", "path"}, item_type)
            name = _required_field_text(value, "name", item_type)
            path = _required_field_text(value, "path", item_type)
            if name == "glob":
                arguments = {"pattern": path}
            else:
                arguments = {"path": path}
            return ToolUse(
                tool_use_id=_required_field_text(value, "tool_use_id", item_type),
                name=name,
                arguments=ToolArguments.from_mapping(arguments),
            )
        _closed_fields(
            value,
            {
                "item_type",
                "tool_use_id",
                "name",
                "arguments_version",
                "arguments",
            },
            item_type,
        )
        arguments_version = value.get("arguments_version")
        if type(arguments_version) is not int:
            raise SessionRecordError("tool_use arguments_version must be an integer")
        raw_arguments = value.get("arguments")
        if not isinstance(raw_arguments, dict):
            raise SessionRecordError("tool_use arguments must be a JSON object")
        try:
            arguments = ToolArguments.from_mapping(
                raw_arguments,
                version=arguments_version,
            )
        except ValueError as error:
            raise SessionRecordError(str(error)) from None
        return ToolUse(
            tool_use_id=_required_field_text(value, "tool_use_id", item_type),
            name=_required_field_text(value, "name", item_type),
            arguments=arguments,
        )
    if item_type == "tool_result":
        _closed_fields(
            value,
            {"item_type", "tool_use_id", "content", "is_error", "truncated"},
            item_type,
        )
        content = _required_field_payload_text(value, "content", item_type)
        is_error = value.get("is_error")
        truncated = value.get("truncated")
        if type(is_error) is not bool or type(truncated) is not bool:
            raise SessionRecordError("tool_result flags must be booleans")
        return ToolResult(
            tool_use_id=_required_field_text(value, "tool_use_id", item_type),
            content=content,
            is_error=is_error,
            truncated=truncated,
        )
    raise SessionRecordError(f"unknown turn item type: {item_type}")


def _validate_header(header: SessionHeader) -> None:
    if header.sequence != 0:
        raise SessionRecordError("session_header sequence must be zero")
    canonical_session_id(header.session_id)
    workspace = Path(header.workspace)
    if not workspace.is_absolute() or str(workspace) != header.workspace:
        raise SessionRecordError("session workspace must be a canonical absolute path")
    if _WORKSPACE_FINGERPRINT.fullmatch(header.workspace_fingerprint) is None:
        raise SessionRecordError("session workspace fingerprint is invalid")
    _validate_timestamp(header.created_at, "session created_at")
    header.binding.__post_init__()


def _validate_turn(items: tuple[ConversationItem, ...], seen_tool_ids: set[str]) -> None:
    for item in items:
        _item_to_dict(item, schema_version=TURN_COMMITTED_SCHEMA_VERSION)
    try:
        validated = validate_complete_history(
            items,
            prior_tool_use_ids=frozenset(seen_tool_ids),
        )
    except ValueError as error:
        raise SessionRecordError(f"invalid committed turn: {error}") from None
    if len(validated.complete_turns) != 1:
        raise SessionRecordError("turn_committed must contain exactly one complete turn")
    seen_tool_ids.update(validated.tool_use_ids)


def _validate_record_version(record: SessionRecord) -> None:
    if isinstance(record, TurnCommitted):
        if record.schema_version not in {
            TURN_COMMITTED_LEGACY_SCHEMA_VERSION,
            TURN_COMMITTED_SCHEMA_VERSION,
        }:
            raise SessionRecordError("unsupported session record schema version")
        return
    if isinstance(record, ContextCompacted):
        expected = {
            CONTEXT_COMPACTED_LEGACY_SCHEMA_VERSION,
            CONTEXT_COMPACTED_SCHEMA_VERSION,
        }
        if record.schema_version not in expected:
            raise SessionRecordError("unsupported session record schema version")
        return
    if record.schema_version != SCHEMA_VERSION:
        raise SessionRecordError("unsupported session record schema version")


def _validate_context_compacted_fields(record: ContextCompacted) -> None:
    _validate_record_version(record)
    if record.schema_version == CONTEXT_COMPACTED_LEGACY_SCHEMA_VERSION:
        if record.trigger != CompactionTrigger.MANUAL or record.high_water_percent is not None:
            raise SessionRecordError(
                "legacy context_compacted provenance must be manual without a threshold"
            )
    elif record.trigger == CompactionTrigger.HIGH_WATER:
        if record.high_water_percent != 80:
            raise SessionRecordError("high-water context_compacted threshold must be 80")
    elif record.trigger in {CompactionTrigger.MANUAL, CompactionTrigger.OVERFLOW}:
        if record.high_water_percent is not None:
            raise SessionRecordError(
                "manual and overflow context_compacted thresholds must be null"
            )
    else:
        raise SessionRecordError("context_compacted trigger is invalid")
    _validate_timestamp(record.occurred_at, "context_compacted occurred_at")
    record.binding.__post_init__()
    _context_id(record.source_context_id, "context_compacted source_context_id")
    _context_id(record.result_context_id, "context_compacted result_context_id")
    for value, label in (
        (record.source_full_turn_count, "source_full_turn_count"),
        (record.source_effective_turn_count, "source_effective_turn_count"),
        (record.retained_from_full_turn, "retained_from_full_turn"),
    ):
        if type(value) is not int or value < 0:
            raise SessionRecordError(f"context_compacted {label} must be non-negative")
    if record.previous_checkpoint_sequence is not None and (
        type(record.previous_checkpoint_sequence) is not int
        or record.previous_checkpoint_sequence < 0
    ):
        raise SessionRecordError(
            "context_compacted previous_checkpoint_sequence must be non-negative or null"
        )
    _text_payload(record.summary, "context_compacted summary")
    if not record.summary.strip():
        raise SessionRecordError("context_compacted summary must not be blank")
    prompt = build_compact_prompt()
    if (
        record.compact_prompt_version != COMPACT_PROMPT_VERSION
        or record.compact_prompt_fingerprint != prompt.fingerprint
    ):
        raise SessionRecordError("context_compacted compact prompt provenance is unsupported")
    if (
        record.continuation_version != SUMMARY_CONTINUATION_VERSION
        or record.continuation_fingerprint
        != summary_continuation_fingerprint(SUMMARY_CONTINUATION_VERSION)
    ):
        raise SessionRecordError("context_compacted continuation provenance is unsupported")
    if (
        record.effective_context_representation_version
        != COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION
    ):
        raise SessionRecordError(
            "context_compacted effective-context representation is unsupported"
        )


def _validate_context_compacted(
    record: ContextCompacted,
    *,
    full_history: tuple[ConversationItem, ...],
    effective_history: tuple[ConversationItem, ...],
    latest_checkpoint: ContextCompacted | None,
) -> None:
    _validate_context_compacted_fields(record)
    full_turns = validate_complete_history(full_history).complete_turns
    effective_turns = validate_complete_history(effective_history).complete_turns
    if record.source_full_turn_count != len(full_turns):
        raise SessionRecordError("context_compacted full turn count does not match replay state")
    if record.source_effective_turn_count != len(effective_turns):
        raise SessionRecordError(
            "context_compacted effective turn count does not match replay state"
        )
    if len(effective_turns) < COMPACT_MIN_EFFECTIVE_TURNS:
        raise SessionRecordError("context_compacted source has too few effective turns")
    expected_boundary = len(full_turns) - COMPACT_RETAINED_TURNS
    if record.retained_from_full_turn != expected_boundary:
        raise SessionRecordError("context_compacted retained boundary is invalid")
    expected_previous = latest_checkpoint.sequence if latest_checkpoint is not None else None
    if record.previous_checkpoint_sequence != expected_previous:
        raise SessionRecordError(
            "context_compacted previous checkpoint does not match replay state"
        )
    if latest_checkpoint is not None and (
        record.retained_from_full_turn < latest_checkpoint.retained_from_full_turn
    ):
        raise SessionRecordError("context_compacted retained boundary moved backwards")


def _context_id(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"ctx-v[12]-[0-9a-f]{64}", value) is None:
        raise SessionRecordError(f"{label} is invalid")


def _closed_fields(value: dict[str, object], expected: set[str], label: str) -> None:
    unknown = set(value) - expected
    if unknown:
        raise SessionRecordError(f"{label} contains unknown field: {sorted(unknown)[0]}")
    missing = expected - set(value)
    if missing:
        raise SessionRecordError(f"{label} is missing required field: {sorted(missing)[0]}")


def _required_field_payload_text(value: dict[str, object], field: str, label: str) -> str:
    """Decode conversation payload text without the 4096-character metadata cap."""
    result = value.get(field)
    if not isinstance(result, str):
        raise SessionRecordError(f"{label} {field} must be text")
    _text_payload(result, f"{label} {field}")
    return result


def _required_field_text(
    value: dict[str, object], field: str, label: str, *, allow_empty: bool = False
) -> str:
    result = value.get(field)
    if not isinstance(result, str):
        raise SessionRecordError(f"{label} {field} must be text")
    _required_text(result, f"{label} {field}", allow_empty=allow_empty)
    return result


def _nullable_field_text(value: dict[str, object], field: str, label: str) -> str | None:
    result = value.get(field)
    if result is None:
        return None
    if not isinstance(result, str):
        raise SessionRecordError(f"{label} {field} must be text or null")
    return result


def _required_field_int(value: dict[str, object], field: str, label: str) -> int:
    result = value.get(field)
    if type(result) is not int:
        raise SessionRecordError(f"{label} {field} must be an integer")
    return result


def _nullable_field_int(value: dict[str, object], field: str, label: str) -> int | None:
    result = value.get(field)
    if result is not None and type(result) is not int:
        raise SessionRecordError(f"{label} {field} must be an integer or null")
    return result


def _nullable_field_number(value: dict[str, object], field: str, label: str) -> float | None:
    result = value.get(field)
    if result is None:
        return None
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise SessionRecordError(f"{label} {field} must be a number or null")
    return float(result)


def _required_text(value: object, label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise SessionRecordError(f"{label} must be text")
    if not allow_empty and not value:
        raise SessionRecordError(f"{label} must not be empty")
    if len(value) > MAX_STRING_LENGTH:
        raise SessionRecordError(f"{label} exceeds {MAX_STRING_LENGTH} characters")
    if "\x00" in value:
        raise SessionRecordError(f"{label} must not contain NUL")


def _optional_text(value: object, label: str) -> None:
    if value is not None:
        _required_text(value, label)


def _text_payload(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise SessionRecordError(f"{label} must be text")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise SessionRecordError(f"{label} exceeds {MAX_TEXT_BYTES} bytes")
    if "\x00" in value:
        raise SessionRecordError(f"{label} must not contain NUL")


def _required_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SessionRecordError(f"{label} must be a lowercase SHA-256 hex digest")


def _optional_sha256(value: object, label: str) -> None:
    if value is not None:
        _required_sha256(value, label)


def _validate_base_url(value: object) -> None:
    if value is None:
        return
    _required_text(value, "binding base_url")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SessionRecordError("binding base_url must be an absolute credential-free HTTP(S) URL")


def _validate_timestamp(value: object, label: str) -> None:
    _required_text(value, label)
    assert isinstance(value, str)
    if not value.endswith("Z"):
        raise SessionRecordError(f"{label} must be a UTC RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise SessionRecordError(f"{label} must be a UTC RFC3339 timestamp") from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise SessionRecordError(f"{label} must be a UTC RFC3339 timestamp")
