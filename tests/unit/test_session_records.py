from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from leonervis_code.core.contracts import AssistantText, ToolResult, ToolUse, UserMessage
from leonervis_code.session_records import (
    BindingSnapshot,
    RuntimeChanged,
    SessionHeader,
    SessionRecordError,
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


def test_binding_rejects_credential_bearing_url_and_non_digest_fingerprint() -> None:
    binding = BindingSnapshot.fake()
    with pytest.raises(SessionRecordError, match="credential-free"):
        replace(binding, base_url="https://user:secret@example.test/v1")
    with pytest.raises(SessionRecordError, match="SHA-256"):
        replace(binding, route_fingerprint="short")
