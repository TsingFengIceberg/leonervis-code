from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from leonervis_code.core.compaction import (
    CompactionTrigger,
    EffectiveContextSummary,
    build_compact_prompt,
)
from leonervis_code.core.contracts import AssistantText, ToolResult, ToolUse, UserMessage
from leonervis_code.session_records import (
    BindingSnapshot,
    ContextCompacted,
    Recovery,
    RuntimeChanged,
    SessionClosed,
    SessionHeader,
    SessionRecordError,
    SessionResumed,
    TurnCommitted,
    decode_record,
    encode_record,
    replay_records,
    workspace_fingerprint,
)

SESSION_ID = "12345678-1234-4234-9234-123456789abc"
NOW = "2026-07-17T12:00:00.000000Z"


def test_record_codec_round_trip_and_replay_excludes_audit(tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    first_binding = BindingSnapshot.fake()
    second_binding = BindingSnapshot.fake(generation=1, source="runtime")
    records = [
        SessionHeader(
            sequence=0,
            session_id=SESSION_ID,
            workspace=str(workspace),
            workspace_fingerprint=workspace_fingerprint(workspace),
            created_at=NOW,
            binding=first_binding,
        ),
        RuntimeChanged(
            sequence=1,
            occurred_at=NOW,
            binding=second_binding,
            reason="model override",
        ),
        TurnCommitted(
            sequence=2,
            committed_at=NOW,
            binding=second_binding,
            items=(
                UserMessage("read it"),
                ToolUse("tool-1", "read_file", "README.md"),
                ToolResult("tool-1", "contents"),
                AssistantText("done"),
            ),
        ),
    ]

    decoded = [decode_record(encode_record(record)) for record in records]
    state = replay_records(
        decoded,
        expected_workspace=str(workspace),
        expected_workspace_fingerprint=workspace_fingerprint(workspace),
        expected_session_id=SESSION_ID,
        expected_file_name=f"{SESSION_ID}.jsonl",
    )

    assert decoded == records
    assert state.history == records[-1].items
    assert state.turns[0].user.text == "read it"
    assert state.binding == second_binding
    assert state.next_sequence == 3


def test_codec_restores_conversation_payloads_larger_than_metadata_limit() -> None:
    long_user = "用" * 5000
    long_assistant = "答" * 6000
    long_result = "结果" * 3000
    turn = TurnCommitted(
        sequence=1,
        committed_at=NOW,
        binding=BindingSnapshot.fake(),
        items=(
            UserMessage(long_user),
            ToolUse("tool-long", "read_file", "README.md"),
            ToolResult("tool-long", long_result),
            AssistantText(long_assistant),
        ),
    )

    decoded = decode_record(encode_record(turn))

    assert decoded == turn


def test_canonical_codec_is_compact_sorted_and_contains_no_secret_value(tmp_path: Path) -> None:
    binding = BindingSnapshot(
        profile_id="profile-id",
        profile_revision=3,
        profile_name="work",
        profile_fingerprint="a" * 64,
        provider_id="custom",
        protocol="openai_chat_completions",
        selected_model="vendor/model",
        wire_model="vendor/model",
        base_url="https://example.test/v1",
        base_url_source="profile",
        source="profile",
        credential_env="API_TOKEN",
        max_output_tokens=4096,
        temperature=0.2,
        generation=7,
        adapter_version="openai-compat-v1",
        route_fingerprint="b" * 64,
    )
    header = SessionHeader(
        sequence=0,
        session_id=SESSION_ID,
        workspace=str(tmp_path.resolve()),
        workspace_fingerprint=workspace_fingerprint(tmp_path),
        created_at=NOW,
        binding=binding,
    )

    line = encode_record(header)

    assert line.endswith(b"\n")
    assert b" " not in line
    assert b"API_TOKEN" in line
    assert b"credential_value" not in line
    assert line == encode_record(decode_record(line))


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value.update(secret="x"), "unknown field"),
        (lambda value: value.update(schema_version=2), "unsupported"),
        (lambda value: value.update(sequence=True), "sequence"),
        (lambda value: value["binding"].update(secret="x"), "unknown field"),
    ],
)
def test_decode_fails_closed_on_unknown_version_and_field_types(
    tmp_path: Path, mutate, match: str
) -> None:
    header = SessionHeader(
        sequence=0,
        session_id=SESSION_ID,
        workspace=str(tmp_path.resolve()),
        workspace_fingerprint=workspace_fingerprint(tmp_path),
        created_at=NOW,
        binding=BindingSnapshot.fake(),
    )
    value = json.loads(encode_record(header))
    mutate(value)

    with pytest.raises(SessionRecordError, match=match):
        decode_record(json.dumps(value).encode())


def test_replay_rejects_sequence_workspace_filename_and_records_after_close(tmp_path: Path) -> None:
    header = SessionHeader(
        sequence=0,
        session_id=SESSION_ID,
        workspace=str(tmp_path.resolve()),
        workspace_fingerprint=workspace_fingerprint(tmp_path),
        created_at=NOW,
        binding=BindingSnapshot.fake(),
    )
    skipped = TurnCommitted(
        sequence=2,
        committed_at=NOW,
        binding=header.binding,
        items=(UserMessage("u"), AssistantText("a")),
    )

    with pytest.raises(SessionRecordError, match="sequence mismatch"):
        replay_records([header, skipped])
    with pytest.raises(SessionRecordError, match="workspace does not match"):
        replay_records([header], expected_workspace="/different")
    with pytest.raises(SessionRecordError, match="file name"):
        replay_records([header], expected_file_name="wrong.jsonl")


def test_recovery_after_close_preserves_closed_state_until_resumed(tmp_path: Path) -> None:
    header = SessionHeader(
        sequence=0,
        session_id=SESSION_ID,
        workspace=str(tmp_path.resolve()),
        workspace_fingerprint=workspace_fingerprint(tmp_path),
        created_at=NOW,
        binding=BindingSnapshot.fake(),
    )
    closed = SessionClosed(sequence=1, occurred_at=NOW, reason="closed")
    recovery = Recovery(sequence=2, occurred_at=NOW, truncated_bytes=12)

    recovered = replay_records([header, closed, recovery])

    assert recovered.closed is True
    with pytest.raises(SessionRecordError, match="requires session_resumed"):
        replay_records(
            [
                header,
                closed,
                recovery,
                TurnCommitted(
                    sequence=3,
                    committed_at=NOW,
                    binding=header.binding,
                    items=(UserMessage("u"), AssistantText("a")),
                ),
            ]
        )
    resumed = replay_records(
        [
            header,
            closed,
            recovery,
            SessionResumed(sequence=3, occurred_at=NOW),
        ]
    )
    assert resumed.closed is False


def test_replay_requires_closed_turns_and_strict_tool_causality(tmp_path: Path) -> None:
    header = SessionHeader(
        sequence=0,
        session_id=SESSION_ID,
        workspace=str(tmp_path.resolve()),
        workspace_fingerprint=workspace_fingerprint(tmp_path),
        created_at=NOW,
        binding=BindingSnapshot.fake(),
    )

    cases = [
        (UserMessage("u"), ToolUse("one", "read_file", "x"), AssistantText("a")),
        (UserMessage("u"), ToolResult("one", "x"), AssistantText("a")),
        (
            UserMessage("u"),
            ToolUse("one", "read_file", "x"),
            ToolResult("one", "x"),
            ToolUse("one", "read_file", "y"),
            ToolResult("one", "y"),
            AssistantText("a"),
        ),
        (
            UserMessage("u"),
            ToolUse("one", "read_file", "x"),
            ToolUse("two", "read_file", "y"),
            ToolResult("two", "y"),
            ToolResult("one", "x"),
            AssistantText("a"),
        ),
        (UserMessage("u"), AssistantText("middle"), AssistantText("a")),
    ]
    for items in cases:
        turn = TurnCommitted(
            sequence=1,
            committed_at=NOW,
            binding=header.binding,
            items=items,
        )
        with pytest.raises(SessionRecordError):
            replay_records([header, turn])


def test_mixed_v1_v2_checkpoint_replay_preserves_full_and_replaces_effective(
    tmp_path: Path,
) -> None:
    workspace = tmp_path.resolve()
    binding = BindingSnapshot.fake()
    header = SessionHeader(
        0,
        SESSION_ID,
        str(workspace),
        workspace_fingerprint(workspace),
        NOW,
        binding,
    )
    turns = [
        TurnCommitted(
            sequence=index,
            committed_at=NOW,
            binding=binding,
            items=(UserMessage(f"u{index}"), AssistantText(f"a{index}")),
        )
        for index in range(1, 5)
    ]
    prompt = build_compact_prompt()
    summary = EffectiveContextSummary("u1 and u2 were resolved")
    checkpoint = ContextCompacted(
        sequence=5,
        occurred_at=NOW,
        binding=binding,
        source_context_id="ctx-v1-" + "a" * 64,
        result_context_id="ctx-v2-" + "b" * 64,
        source_full_turn_count=4,
        source_effective_turn_count=4,
        retained_from_full_turn=2,
        previous_checkpoint_sequence=None,
        summary=summary.text,
        compact_prompt_version=prompt.version,
        compact_prompt_fingerprint=prompt.fingerprint,
        continuation_version=summary.continuation_version,
        continuation_fingerprint=summary.continuation_fingerprint,
        effective_context_representation_version=2,
        schema_version=2,
    )

    encoded_v1_prefix = b"".join(encode_record(record) for record in [header, *turns])
    decoded = [decode_record(encode_record(record)) for record in [header, *turns, checkpoint]]
    state = replay_records(decoded)

    assert b"".join(encode_record(record) for record in decoded[:5]) == encoded_v1_prefix
    assert len(state.turns) == 4
    assert len(state.history) == 8
    assert state.effective_history == turns[2].items + turns[3].items
    assert state.effective_summary == summary
    assert state.latest_checkpoint == checkpoint
    assert state.effective_source == "compact_checkpoint"

    value = json.loads(encode_record(checkpoint))
    value["schema_version"] = 1
    with pytest.raises(SessionRecordError, match="unsupported"):
        decode_record(json.dumps(value).encode())


def test_context_compacted_v3_persists_trigger_and_validates_combinations(
    tmp_path: Path,
) -> None:
    workspace = tmp_path.resolve()
    binding = BindingSnapshot.fake()
    header = SessionHeader(
        0,
        SESSION_ID,
        str(workspace),
        workspace_fingerprint(workspace),
        NOW,
        binding,
    )
    turns = [
        TurnCommitted(
            sequence=index,
            committed_at=NOW,
            binding=binding,
            items=(UserMessage(f"u{index}"), AssistantText(f"a{index}")),
        )
        for index in range(1, 5)
    ]
    prompt = build_compact_prompt()
    checkpoint = ContextCompacted(
        sequence=5,
        occurred_at=NOW,
        binding=binding,
        source_context_id="ctx-v1-" + "a" * 64,
        result_context_id="ctx-v2-" + "b" * 64,
        source_full_turn_count=4,
        source_effective_turn_count=4,
        retained_from_full_turn=2,
        previous_checkpoint_sequence=None,
        summary="summary",
        compact_prompt_version=prompt.version,
        compact_prompt_fingerprint=prompt.fingerprint,
        continuation_version=EffectiveContextSummary("summary").continuation_version,
        continuation_fingerprint=EffectiveContextSummary("summary").continuation_fingerprint,
        effective_context_representation_version=2,
        trigger=CompactionTrigger.HIGH_WATER,
        high_water_percent=80,
    )

    decoded = decode_record(encode_record(checkpoint))
    assert decoded == checkpoint
    state = replay_records([header, *turns, decoded])
    assert state.latest_checkpoint.trigger == CompactionTrigger.HIGH_WATER

    with pytest.raises(SessionRecordError, match="threshold"):
        encode_record(replace(checkpoint, high_water_percent=70))
    with pytest.raises(SessionRecordError, match="threshold"):
        encode_record(
            replace(
                checkpoint,
                trigger=CompactionTrigger.OVERFLOW,
                high_water_percent=80,
            )
        )

    binding = BindingSnapshot.fake()
    with pytest.raises(SessionRecordError, match="credential-free"):
        replace(binding, base_url="https://user:secret@example.test/v1")
    with pytest.raises(SessionRecordError, match="SHA-256"):
        replace(binding, route_fingerprint="short")
