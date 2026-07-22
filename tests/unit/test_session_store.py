from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import stat
from threading import Barrier, Thread
from uuid import UUID

import pytest

from leonervis_code.core.contracts import AssistantText, ToolResult, ToolUse, UserMessage
from leonervis_code.session_records import BindingSnapshot
from leonervis_code.session_store import (
    AtomicJsonWriteError,
    SessionLockedError,
    SessionResumeStaleError,
    SessionStore,
    SessionStoreError,
)

SESSION_ONE = "12345678-1234-4234-9234-123456789abc"
SESSION_TWO = "22345678-1234-4234-9234-123456789abc"
NOW = "2026-07-17T12:00:00.000000Z"


def store(workspace: Path, session_id: str = SESSION_ONE) -> SessionStore:
    return SessionStore(
        workspace,
        uuid_factory=lambda: UUID(session_id),
        clock=lambda: NOW,
    )


def committed_items(tool_id: str = "tool-1"):
    return (
        UserMessage("read"),
        ToolUse(tool_id, "read_file", "README.md"),
        ToolResult(tool_id, "content"),
        AssistantText("done"),
    )


def test_create_append_release_open_latest_round_trip_and_list(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    binding = BindingSnapshot.fake()
    writer = session_store.create(binding)

    assert writer.path == session_store.root / f"{SESSION_ONE}.jsonl"
    assert writer.path.parent == (
        tmp_path / ".leonervis-code" / "sessions" / session_store.workspace_fingerprint
    )
    writer.append_turn(committed_items(), binding=binding)
    writer.turn_failed(binding=binding, failure_kind="cancelled", message="user cancelled")
    assert len(writer.state.history) == 4
    assert len(writer.state.turns) == 1
    writer.release()

    reopened = session_store.open("latest")
    assert reopened.session_id == SESSION_ONE
    assert reopened.state.records[-1].record_type == "session_resumed"
    assert reopened.state.history == committed_items()
    reopened.close(reason="done")

    info = session_store.show(SESSION_ONE)
    assert info.closed is True
    assert info.turn_count == 1
    assert session_store.list() == (info,)

    resumed_after_clean_close = session_store.open(SESSION_ONE)
    assert resumed_after_clean_close.state.closed is False
    assert resumed_after_clean_close.state.history == committed_items()
    resumed_after_clean_close.release()


def test_prepare_resume_is_read_only_and_abort_releases_target_lock(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.append_turn(committed_items(), binding=BindingSnapshot.fake())
    writer.release()
    transcript_before = writer.path.read_bytes()
    latest = session_store.root / "latest.json"
    latest_before = latest.read_bytes()

    prepared = session_store.prepare_resume("latest")

    assert prepared.state.history == committed_items()
    assert writer.path.read_bytes() == transcript_before
    assert latest.read_bytes() == latest_before
    with pytest.raises(SessionLockedError):
        session_store.prepare_resume(SESSION_ONE)

    prepared.abort()
    reopened = session_store.prepare_resume(SESSION_ONE)
    reopened.abort()


def test_prepare_resume_defers_tail_recovery_until_commit(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.append_turn(committed_items(), binding=BindingSnapshot.fake())
    writer.release()
    partial = b'{"record_type":"turn_comm'
    writer.path.write_bytes(writer.path.read_bytes() + partial)
    before = writer.path.read_bytes()

    prepared = session_store.prepare_resume(SESSION_ONE)

    assert prepared.pending_recovery is not None
    assert writer.path.read_bytes() == before
    committed = prepared.commit()
    assert [record.record_type for record in committed.writer.state.records[-2:]] == [
        "recovery",
        "session_resumed",
    ]
    committed.writer.release()


def test_prepare_resume_detects_exact_transcript_staleness(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.release()
    prepared = session_store.prepare_resume(SESSION_ONE)
    original = writer.path.read_bytes()
    changed = bytearray(original)
    changed[-2] = ord(" ") if changed[-2] != ord(" ") else ord("x")
    writer.path.write_bytes(changed)

    with pytest.raises(SessionResumeStaleError):
        prepared.commit()
    prepared.abort()


def test_latest_resume_uses_exact_pointer_cas_but_explicit_id_does_not(
    tmp_path: Path,
) -> None:
    session_store = store(tmp_path)
    first = session_store.create(BindingSnapshot.fake())
    first.release()
    prepared_latest = session_store.prepare_resume("latest")
    latest = session_store.root / "latest.json"
    latest_before = latest.read_bytes()
    latest.write_bytes(latest_before.replace(SESSION_ONE.encode(), SESSION_TWO.encode()))

    with pytest.raises(SessionResumeStaleError, match="latest Session changed"):
        prepared_latest.commit()
    prepared_latest.abort()

    latest.write_bytes(latest_before)
    prepared_explicit = session_store.prepare_resume(SESSION_ONE)
    latest.write_bytes(latest_before.replace(SESSION_ONE.encode(), SESSION_TWO.encode()))
    committed = prepared_explicit.commit()

    assert committed.writer.session_id == SESSION_ONE
    assert session_store.show("latest").session_id == SESSION_ONE
    committed.writer.release()


def test_prepare_resume_detects_lock_path_replacement(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.release()
    prepared = session_store.prepare_resume(SESSION_ONE)
    replacement = prepared.lock_path.with_suffix(".replacement")
    replacement.write_bytes(b"")
    os.replace(replacement, prepared.lock_path)

    with pytest.raises(SessionResumeStaleError, match="lock changed"):
        prepared.commit()
    prepared.abort()


def test_create_keeps_transcript_if_latest_was_replaced_before_fsync_failure(
    monkeypatch, tmp_path: Path
) -> None:
    session_store = store(tmp_path)

    def fail_after_replace(path, data):
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": SESSION_ONE,
                    "transcript": f"{SESSION_ONE}.jsonl",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        raise AtomicJsonWriteError("directory fsync failed", replaced=True)

    monkeypatch.setattr(
        session_store,
        "_write_latest",
        lambda session_id: fail_after_replace(session_store.root / "latest.json", session_id),
    )

    with pytest.raises(AtomicJsonWriteError) as caught:
        session_store.create(BindingSnapshot.fake())

    assert caught.value.replaced is True
    assert (session_store.root / f"{SESSION_ONE}.jsonl").is_file()
    assert (session_store.root / f"{SESSION_ONE}.lock").is_file()
    assert session_store.show("latest").session_id == SESSION_ONE


def test_create_is_collision_safe_and_latest_does_not_fallback(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.release()
    with pytest.raises(SessionStoreError, match="collision"):
        session_store.create(BindingSnapshot.fake())

    latest = session_store.root / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": SESSION_TWO,
                "transcript": f"{SESSION_TWO}.jsonl",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SessionStoreError, match="does not exist"):
        session_store.show("latest")


def test_selectors_reject_noncanonical_ids_and_paths_outside_root(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.release()

    with pytest.raises(SessionStoreError, match="canonical UUID4"):
        session_store.show(SESSION_ONE.upper())
    with pytest.raises(SessionStoreError, match="directly inside"):
        session_store.show(tmp_path / f"{SESSION_ONE}.jsonl")
    with pytest.raises(SessionStoreError, match="directly inside"):
        session_store.show(session_store.root / "subdir" / f"{SESSION_ONE}.jsonl")
    assert session_store.show(session_store.root / f"{SESSION_ONE}.jsonl").session_id == SESSION_ONE


def test_open_repairs_only_incomplete_final_tail_and_appends_recovery(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.append_turn(committed_items(), binding=BindingSnapshot.fake())
    writer.release()
    original = writer.path.read_bytes()
    writer.path.write_bytes(original + b'{"record_type":"turn_comm')

    reopened = session_store.open(SESSION_ONE)

    assert [record.record_type for record in reopened.state.records[-2:]] == [
        "recovery",
        "session_resumed",
    ]
    recovery = reopened.state.records[-2]
    assert recovery.truncated_bytes == len(b'{"record_type":"turn_comm')
    assert reopened.state.history == committed_items()
    assert reopened.path.read_bytes().endswith(b"\n")
    reopened.release()


def test_complete_json_without_newline_is_not_repaired(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.release()
    writer.path.write_bytes(writer.path.read_bytes() + b"{}")

    before = writer.path.read_bytes()
    with pytest.raises(SessionStoreError, match="complete JSON record without a newline"):
        session_store.open(SESSION_ONE)
    assert writer.path.read_bytes() == before


@pytest.mark.parametrize(
    "corruption",
    [
        b"not-json\n",
        json.dumps({"record_type": "unknown", "schema_version": 1, "sequence": 1}).encode() + b"\n",
        json.dumps(
            {
                "record_type": "session_resumed",
                "schema_version": 2,
                "sequence": 1,
                "occurred_at": NOW,
            }
        ).encode()
        + b"\n",
        json.dumps(
            {
                "record_type": "session_resumed",
                "schema_version": 1,
                "sequence": 9,
                "occurred_at": NOW,
            }
        ).encode()
        + b"\n",
    ],
)
def test_newline_terminated_corruption_fails_closed_without_repair(
    tmp_path: Path, corruption: bytes
) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.release()
    writer.path.write_bytes(writer.path.read_bytes() + corruption)
    before = writer.path.read_bytes()

    with pytest.raises(SessionStoreError):
        session_store.open(SESSION_ONE)
    assert writer.path.read_bytes() == before


def test_middle_corruption_is_never_repaired(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    writer.append_turn(committed_items(), binding=BindingSnapshot.fake())
    writer.release()
    lines = writer.path.read_bytes().splitlines(keepends=True)
    writer.path.write_bytes(lines[0] + b"broken\n" + lines[1] + b"partial")
    before = writer.path.read_bytes()

    with pytest.raises(SessionStoreError, match="record 2"):
        session_store.open(SESSION_ONE)
    assert writer.path.read_bytes() == before


def test_workspace_mismatch_filename_mismatch_symlink_and_nonregular_are_rejected(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_store = store(first)
    writer = first_store.create(BindingSnapshot.fake())
    writer.release()

    second_store = store(second, SESSION_TWO)
    second_store.root.mkdir(parents=True)
    stolen = second_store.root / f"{SESSION_ONE}.jsonl"
    stolen.write_bytes(writer.path.read_bytes())
    with pytest.raises(SessionStoreError, match="workspace does not match"):
        second_store.show(stolen)

    renamed = first_store.root / f"{SESSION_TWO}.jsonl"
    renamed.write_bytes(writer.path.read_bytes())
    with pytest.raises(SessionStoreError, match="session ID does not match"):
        first_store.show(renamed)

    target = tmp_path / "target.jsonl"
    target.write_bytes(writer.path.read_bytes())
    symlink = first_store.root / "32345678-1234-4234-9234-123456789abc.jsonl"
    symlink.symlink_to(target)
    with pytest.raises(SessionStoreError, match="symlink"):
        first_store.show(symlink)

    nonregular = first_store.root / "42345678-1234-4234-9234-123456789abc.jsonl"
    nonregular.mkdir()
    with pytest.raises(SessionStoreError, match="regular file"):
        first_store.show(nonregular)


def test_session_directory_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    control = tmp_path / ".leonervis-code"
    control.mkdir()
    (control / "sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SessionStoreError, match="symlink"):
        store(tmp_path).create(BindingSnapshot.fake())


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertions")
def test_storage_permissions_are_private(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())

    assert mode(tmp_path / ".leonervis-code") == 0o700
    assert mode(tmp_path / ".leonervis-code" / "sessions") == 0o700
    assert mode(session_store.root) == 0o700
    assert mode(writer.path) == 0o600
    assert mode(writer.lock_path) == 0o600
    assert mode(session_store.root / "latest.json") == 0o600
    assert mode(session_store.root / ".directory.lock") == 0o600
    writer.release()


def test_lifetime_lock_is_nonblocking_in_threads_and_other_sessions_can_open(
    tmp_path: Path,
) -> None:
    first_store = store(tmp_path, SESSION_ONE)
    first = first_store.create(BindingSnapshot.fake())
    second_store = store(tmp_path, SESSION_TWO)
    second = second_store.create(BindingSnapshot.fake())
    barrier = Barrier(2)
    errors: list[Exception] = []

    def contend() -> None:
        barrier.wait()
        try:
            first_store.open(SESSION_ONE)
        except Exception as error:
            errors.append(error)

    thread = Thread(target=contend)
    thread.start()
    barrier.wait()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], SessionLockedError)
    second.append_turn(committed_items("tool-2"), binding=BindingSnapshot.fake())
    first.release()
    second.release()


@pytest.mark.skipif(os.name == "nt", reason="process flock test uses fork")
def test_lifetime_lock_is_nonblocking_across_processes(tmp_path: Path) -> None:
    session_store = store(tmp_path)
    writer = session_store.create(BindingSnapshot.fake())
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_try_open_process, args=(tmp_path, queue))
    process.start()
    process.join(timeout=5)

    assert process.exitcode == 0
    assert queue.get(timeout=1) == "locked"
    writer.release()


def _try_open_process(workspace: Path, queue) -> None:
    try:
        store(workspace).open(SESSION_ONE)
    except SessionLockedError:
        queue.put("locked")
    except Exception as error:
        queue.put(type(error).__name__)
    else:
        queue.put("opened")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)
