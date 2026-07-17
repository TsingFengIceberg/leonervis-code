"""Secure append-only storage for schema-v1 Leonervis Code sessions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from threading import Lock
from typing import BinaryIO
from uuid import UUID, uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from leonervis_code.core.contracts import ConversationItem
from leonervis_code.session_records import (
    AuditRecord,
    BindingSnapshot,
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    Recovery,
    ReplayState,
    RuntimeChanged,
    SessionClosed,
    SessionHeader,
    SessionRecord,
    SessionRecordError,
    SessionResumed,
    TurnCommitted,
    TurnFailed,
    canonical_session_id,
    decode_record,
    encode_record,
    replay_records,
    workspace_fingerprint,
)

MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
LATEST_SCHEMA_VERSION = 1
_DIRECTORY_LOCK_NAME = ".directory.lock"
_LATEST_NAME = "latest.json"


class SessionStoreError(RuntimeError):
    """Raised when session persistence cannot proceed safely."""


class SessionLockedError(SessionStoreError):
    """Raised when another writer already owns a session."""


@dataclass(frozen=True)
class SessionInfo:
    """Validated, redacted metadata for one stored session."""

    session_id: str
    path: Path
    workspace: str
    workspace_fingerprint: str
    created_at: str
    record_count: int
    turn_count: int
    closed: bool
    binding: BindingSnapshot


_ACTIVE_WRITERS: set[str] = set()
_ACTIVE_WRITERS_GUARD = Lock()


def utc_now() -> str:
    """Return a canonical UTC timestamp suitable for a transcript record."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class SessionStore:
    """Create, select, validate, and exclusively open workspace-bound sessions."""

    def __init__(
        self,
        workspace: Path,
        *,
        uuid_factory: Callable[[], UUID | str] = uuid4,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        requested = Path(workspace)
        if requested.is_symlink():
            raise SessionStoreError("workspace must not be a symlink")
        try:
            resolved = requested.resolve(strict=True)
        except OSError:
            raise SessionStoreError(
                f"workspace does not exist or is inaccessible: {requested}"
            ) from None
        if not resolved.is_dir():
            raise SessionStoreError(f"workspace is not a directory: {resolved}")
        self.workspace = resolved
        self.workspace_fingerprint = workspace_fingerprint(resolved)
        self.root = resolved / ".leonervis-code" / "sessions" / self.workspace_fingerprint
        self._uuid_factory = uuid_factory
        self._clock = clock

    def create(self, binding: BindingSnapshot) -> SessionWriter:
        """Create a collision-safe transcript, update latest, and keep its writer lock."""
        self._ensure_root()
        with self._directory_lock():
            session_id = _factory_session_id(self._uuid_factory)
            transcript_path = self.root / f"{session_id}.jsonl"
            lock_path = self.root / f"{session_id}.lock"
            if transcript_path.exists() or transcript_path.is_symlink():
                raise SessionStoreError(f"session ID collision: {session_id}")
            lock_stream = self._acquire_writer_lock(lock_path, create_exclusive=True)
            try:
                header = SessionHeader(
                    sequence=0,
                    session_id=session_id,
                    workspace=str(self.workspace),
                    workspace_fingerprint=self.workspace_fingerprint,
                    created_at=self._clock(),
                    binding=binding,
                )
                _create_transcript(transcript_path, encode_record(header))
                self._write_latest(session_id)
            except Exception:
                lock_stream.close()
                _release_active_writer(lock_path)
                try:
                    transcript_path.unlink()
                except OSError:
                    pass
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                raise
        state = replay_records(
            [header],
            expected_workspace=str(self.workspace),
            expected_workspace_fingerprint=self.workspace_fingerprint,
            expected_session_id=session_id,
            expected_file_name=transcript_path.name,
        )
        return SessionWriter(self, transcript_path, lock_path, lock_stream, state)

    def open(self, selector: str | Path) -> SessionWriter:
        """Exclusively open latest, a strict UUID, or a path contained in this session root."""
        self._ensure_root()
        path = self._select_path(selector)
        session_id = _session_id_from_path(path)
        lock_path = self.root / f"{session_id}.lock"
        lock_stream = self._acquire_writer_lock(lock_path, create_exclusive=False)
        try:
            state = self._load_state(path, allow_repair=True)
            resumed = SessionResumed(sequence=state.next_sequence, occurred_at=self._clock())
            _append_record(path, resumed)
            state = replay_records(
                [*state.records, resumed],
                expected_workspace=str(self.workspace),
                expected_workspace_fingerprint=self.workspace_fingerprint,
                expected_session_id=session_id,
                expected_file_name=path.name,
            )
            with self._directory_lock():
                self._write_latest(session_id)
            return SessionWriter(self, path, lock_path, lock_stream, state)
        except Exception:
            lock_stream.close()
            _release_active_writer(lock_path)
            raise

    def show(self, selector: str | Path) -> SessionInfo:
        """Strictly validate and describe a session without repairing or updating it."""
        self._ensure_root()
        path = self._select_path(selector)
        return _info(path, self._load_state(path, allow_repair=False))

    def list(self) -> tuple[SessionInfo, ...]:
        """Return all strictly validated transcripts, newest first."""
        if not self.root.exists() and not self.root.is_symlink():
            return ()
        self._ensure_root()
        infos: list[SessionInfo] = []
        try:
            entries = tuple(self.root.iterdir())
        except OSError:
            raise SessionStoreError(f"could not list session directory: {self.root}") from None
        for path in entries:
            if path.name.endswith(".jsonl"):
                _session_id_from_path(path)
                infos.append(_info(path, self._load_state(path, allow_repair=False)))
        return tuple(
            sorted(infos, key=lambda item: (item.created_at, item.session_id), reverse=True)
        )

    def _load_state(self, path: Path, *, allow_repair: bool) -> ReplayState:
        _ensure_contained_file(path, self.root, suffix=".jsonl")
        try:
            size = path.stat().st_size
        except OSError:
            raise SessionStoreError(f"session transcript is inaccessible: {path}") from None
        if size > MAX_TRANSCRIPT_BYTES:
            raise SessionStoreError(
                f"session transcript exceeds {MAX_TRANSCRIPT_BYTES} bytes: {path}"
            )
        try:
            data = path.read_bytes()
        except OSError:
            raise SessionStoreError(f"could not read session transcript: {path}") from None
        if len(data) != size:
            raise SessionStoreError("session transcript changed while it was being read")

        repaired: Recovery | None = None
        if data and not data.endswith(b"\n"):
            tail_start = data.rfind(b"\n") + 1
            tail = data[tail_start:]
            try:
                json.loads(tail.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                if not allow_repair or tail_start == 0:
                    raise SessionStoreError(
                        "session transcript has an incomplete final record"
                    ) from None
                prefix = data[:tail_start]
                preliminary = _decode_lines(prefix)
                preliminary_state = self._replay(path, preliminary)
                repaired = Recovery(
                    sequence=preliminary_state.next_sequence,
                    occurred_at=self._clock(),
                    truncated_bytes=len(tail),
                )
                _truncate_and_append_recovery(path, tail_start, repaired)
                data = prefix + encode_record(repaired)
            else:
                raise SessionStoreError(
                    "session transcript ends with a complete JSON record without a newline"
                )

        records = _decode_lines(data)
        if repaired is not None and (not records or records[-1] != repaired):
            raise SessionStoreError("session recovery record was not persisted correctly")
        return self._replay(path, records)

    def _replay(self, path: Path, records: list[SessionRecord]) -> ReplayState:
        try:
            return replay_records(
                records,
                expected_workspace=str(self.workspace),
                expected_workspace_fingerprint=self.workspace_fingerprint,
                expected_session_id=_session_id_from_path(path),
                expected_file_name=path.name,
            )
        except SessionRecordError as error:
            raise SessionStoreError(f"invalid session transcript {path}: {error}") from None

    def _select_path(self, selector: str | Path) -> Path:
        if isinstance(selector, Path):
            return _validated_selected_path(selector, self.root)
        if not isinstance(selector, str):
            raise SessionStoreError("session selector must be latest, a UUID, or a path")
        if selector == "latest":
            return self._read_latest()
        if "/" in selector or "\\" in selector or selector.endswith(".jsonl"):
            return _validated_selected_path(Path(selector), self.root)
        try:
            session_id = canonical_session_id(selector)
        except SessionRecordError as error:
            raise SessionStoreError(str(error)) from None
        return _validated_selected_path(self.root / f"{session_id}.jsonl", self.root)

    def _read_latest(self) -> Path:
        path = self.root / _LATEST_NAME
        _ensure_contained_file(path, self.root, suffix=".json")
        try:
            if path.stat().st_size > MAX_RECORD_BYTES:
                raise SessionStoreError("latest session metadata is oversized")
            value = json.loads(path.read_text(encoding="utf-8"))
        except SessionStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SessionStoreError("latest session metadata is unreadable or invalid") from None
        if not isinstance(value, dict):
            raise SessionStoreError("latest session metadata must be a JSON object")
        expected = {"schema_version", "session_id", "transcript"}
        if set(value) != expected or value.get("schema_version") != LATEST_SCHEMA_VERSION:
            raise SessionStoreError("latest session metadata has an unsupported schema")
        session_id_value = value.get("session_id")
        transcript = value.get("transcript")
        try:
            session_id = canonical_session_id(session_id_value)
        except SessionRecordError as error:
            raise SessionStoreError(f"invalid latest session target: {error}") from None
        if transcript != f"{session_id}.jsonl":
            raise SessionStoreError("latest session target does not match its session ID")
        return _validated_selected_path(self.root / transcript, self.root)

    def _write_latest(self, session_id: str) -> None:
        canonical_session_id(session_id)
        data = {
            "schema_version": LATEST_SCHEMA_VERSION,
            "session_id": session_id,
            "transcript": f"{session_id}.jsonl",
        }
        _atomic_json_write(self.root / _LATEST_NAME, data)

    def _ensure_root(self) -> None:
        _ensure_directory(self.workspace / ".leonervis-code", boundary=self.workspace)
        _ensure_directory(self.workspace / ".leonervis-code" / "sessions", boundary=self.workspace)
        _ensure_directory(self.root, boundary=self.workspace)

    @contextmanager
    def _directory_lock(self) -> Iterator[None]:
        path = self.root / _DIRECTORY_LOCK_NAME
        stream = _open_lock(path, exclusive_create=False)
        try:
            _lock_stream(stream, nonblocking=False)
            yield
        finally:
            _unlock_stream(stream)
            stream.close()

    def _acquire_writer_lock(self, path: Path, *, create_exclusive: bool) -> BinaryIO:
        key = str(path)
        with _ACTIVE_WRITERS_GUARD:
            if key in _ACTIVE_WRITERS:
                raise SessionLockedError(f"session already has an active writer: {path.stem}")
            _ACTIVE_WRITERS.add(key)
        try:
            stream = _open_lock(path, exclusive_create=create_exclusive)
            _lock_stream(stream, nonblocking=True)
            return stream
        except SessionLockedError:
            _release_active_writer(path)
            raise
        except Exception:
            _release_active_writer(path)
            raise


class SessionWriter:
    """Lifetime-exclusive append handle for one validated session transcript."""

    def __init__(
        self,
        store: SessionStore,
        path: Path,
        lock_path: Path,
        lock_stream: BinaryIO,
        state: ReplayState,
    ) -> None:
        self._store = store
        self.path = path
        self.lock_path = lock_path
        self._lock_stream = lock_stream
        self._state = state
        self._released = False

    @property
    def session_id(self) -> str:
        return self._state.header.session_id

    @property
    def state(self) -> ReplayState:
        return self._state

    @property
    def info(self) -> SessionInfo:
        return _info(self.path, self._state)

    def append_turn(
        self,
        items: Iterable[ConversationItem],
        *,
        binding: BindingSnapshot,
        committed_at: str | None = None,
    ) -> TurnCommitted:
        """Durably commit one complete turn as exactly one JSONL record."""
        self._ensure_writable()
        record = TurnCommitted(
            sequence=self._state.next_sequence,
            committed_at=committed_at or self._store._clock(),
            binding=binding,
            items=tuple(items),
        )
        self._append(record)
        return record

    def append_audit(self, record: AuditRecord) -> AuditRecord:
        """Append one typed audit event; audit events never enter replay history."""
        self._ensure_writable()
        if isinstance(record, Recovery):
            raise SessionStoreError("recovery records are reserved for automatic tail repair")
        if record.sequence != self._state.next_sequence:
            raise SessionStoreError(
                f"audit sequence must be {self._state.next_sequence}, got {record.sequence}"
            )
        if isinstance(record, SessionClosed):
            raise SessionStoreError("use close() to append session_closed and release the lock")
        self._append(record)
        return record

    def runtime_changed(
        self, binding: BindingSnapshot, *, reason: str, occurred_at: str | None = None
    ) -> RuntimeChanged:
        """Convenience API for a typed runtime_changed audit event."""
        record = RuntimeChanged(
            sequence=self._state.next_sequence,
            occurred_at=occurred_at or self._store._clock(),
            binding=binding,
            reason=reason,
        )
        self.append_audit(record)
        return record

    def turn_failed(
        self,
        *,
        binding: BindingSnapshot,
        failure_kind: str,
        message: str,
        occurred_at: str | None = None,
    ) -> TurnFailed:
        """Convenience API for a typed turn_failed audit event."""
        record = TurnFailed(
            sequence=self._state.next_sequence,
            occurred_at=occurred_at or self._store._clock(),
            binding=binding,
            failure_kind=failure_kind,
            message=message,
        )
        self.append_audit(record)
        return record

    def close(self, *, reason: str = "closed", occurred_at: str | None = None) -> None:
        """Append session_closed once, fsync it, then release the writer lock."""
        if self._released:
            return
        try:
            if not self._state.closed:
                record = SessionClosed(
                    sequence=self._state.next_sequence,
                    occurred_at=occurred_at or self._store._clock(),
                    reason=reason,
                )
                self._append(record)
        finally:
            self._release()

    def release(self) -> None:
        """Release the writer without closing the durable session (for process handoff)."""
        self._release()

    def __enter__(self) -> SessionWriter:
        self._ensure_writable()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(reason="error" if exc_type is not None else "closed")

    def _append(self, record: SessionRecord) -> None:
        candidate = replay_records(
            [*self._state.records, record],
            expected_workspace=str(self._store.workspace),
            expected_workspace_fingerprint=self._store.workspace_fingerprint,
            expected_session_id=self.session_id,
            expected_file_name=self.path.name,
        )
        _append_record(self.path, record)
        self._state = candidate

    def _ensure_writable(self) -> None:
        if self._released:
            raise SessionStoreError("session writer is released")
        if self._state.closed:
            raise SessionStoreError("session is closed")

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            _unlock_stream(self._lock_stream)
        finally:
            self._lock_stream.close()
            _release_active_writer(self.lock_path)


def _factory_session_id(factory: Callable[[], UUID | str]) -> str:
    value = factory()
    candidate = str(value) if isinstance(value, UUID) else value
    try:
        return canonical_session_id(candidate)
    except SessionRecordError as error:
        raise SessionStoreError(f"UUID factory returned an invalid session ID: {error}") from None


def _decode_lines(data: bytes) -> list[SessionRecord]:
    if not data:
        raise SessionStoreError("session transcript is empty")
    if len(data) > MAX_TRANSCRIPT_BYTES:
        raise SessionStoreError(f"session transcript exceeds {MAX_TRANSCRIPT_BYTES} bytes")
    lines = data.splitlines(keepends=True)
    if len(lines) > MAX_RECORDS:
        raise SessionStoreError(f"session transcript exceeds {MAX_RECORDS} records")
    records: list[SessionRecord] = []
    for number, line in enumerate(lines, start=1):
        if not line.endswith(b"\n"):
            raise SessionStoreError(f"session record {number} is missing its newline")
        try:
            records.append(decode_record(line))
        except SessionRecordError as error:
            raise SessionStoreError(f"invalid session record {number}: {error}") from None
    return records


def _create_transcript(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    except FileExistsError:
        raise SessionStoreError(f"session transcript already exists: {path.name}") from None
    except OSError:
        raise SessionStoreError(f"could not create session transcript: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _append_record(path: Path, record: SessionRecord) -> None:
    payload = encode_record(record)
    flags = os.O_WRONLY | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SessionStoreError(f"session transcript is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
        if info.st_size + len(payload) > MAX_TRANSCRIPT_BYTES:
            raise SessionStoreError(
                f"session transcript would exceed {MAX_TRANSCRIPT_BYTES} bytes: {path}"
            )
        with os.fdopen(descriptor, "ab") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except SessionStoreError:
        raise
    except OSError:
        raise SessionStoreError(f"could not append session transcript: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _truncate_and_append_recovery(path: Path, offset: int, record: Recovery) -> None:
    payload = encode_record(record)
    flags = os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SessionStoreError(f"session transcript is not a regular file: {path}")
        os.ftruncate(descriptor, offset)
        os.lseek(descriptor, 0, os.SEEK_END)
        with os.fdopen(descriptor, "ab") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except SessionStoreError:
        raise
    except OSError:
        raise SessionStoreError(f"could not repair session transcript: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_json_write(path: Path, data: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        _ensure_contained_file(path, path.parent, suffix=".json")
    payload = (
        json.dumps(
            data, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )
    temporary: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".latest.", suffix=".tmp")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError:
        raise SessionStoreError(f"could not update latest session metadata: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _validated_selected_path(path: Path, root: Path) -> Path:
    candidate = path if path.is_absolute() else Path.cwd() / path
    absolute = candidate.absolute()
    root_absolute = root.absolute()
    if absolute.parent != root_absolute:
        raise SessionStoreError("session path must be directly inside the current session root")
    _session_id_from_path(absolute)
    _ensure_contained_file(absolute, root_absolute, suffix=".jsonl")
    return absolute


def _session_id_from_path(path: Path) -> str:
    if path.suffix != ".jsonl":
        raise SessionStoreError("session transcript file name must end in .jsonl")
    try:
        return canonical_session_id(path.stem)
    except SessionRecordError as error:
        raise SessionStoreError(f"invalid session transcript file name: {error}") from None


def _ensure_contained_file(path: Path, root: Path, *, suffix: str) -> None:
    if path.parent.absolute() != root.absolute() or path.suffix != suffix:
        raise SessionStoreError("session path escapes the current session root")
    if path.is_symlink():
        raise SessionStoreError(f"session path must not be a symlink: {path}")
    try:
        info = path.lstat()
    except OSError:
        raise SessionStoreError(f"session file does not exist or is inaccessible: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise SessionStoreError(f"session path is not a regular file: {path}")
    try:
        os.chmod(path, 0o600)
    except OSError:
        raise SessionStoreError(f"could not secure session file: {path}") from None


def _ensure_directory(path: Path, *, boundary: Path) -> None:
    if boundary not in path.parents and path != boundary:
        raise SessionStoreError("session directory escapes the workspace")
    relative = path.relative_to(boundary)
    current = boundary
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SessionStoreError(f"session directory must not be a symlink: {current}")
        if current.exists():
            try:
                info = current.lstat()
            except OSError:
                raise SessionStoreError(f"session directory is inaccessible: {current}") from None
            if not stat.S_ISDIR(info.st_mode):
                raise SessionStoreError(f"session directory path is not a directory: {current}")
        else:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise SessionStoreError(f"session directory is unsafe: {current}") from None
            except OSError:
                raise SessionStoreError(f"could not create session directory: {current}") from None
        try:
            os.chmod(current, 0o700)
        except OSError:
            raise SessionStoreError(f"could not secure session directory: {current}") from None


def _open_lock(path: Path, *, exclusive_create: bool) -> BinaryIO:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise SessionStoreError(f"session lock directory is unsafe: {path.parent}")
    if path.is_symlink():
        raise SessionStoreError(f"session lock must not be a symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT
    if exclusive_create:
        flags |= os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SessionStoreError(f"session lock is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "a+b")
        descriptor = None
        return stream
    except FileExistsError:
        raise SessionStoreError(f"session ID collision: {path.stem}") from None
    except SessionStoreError:
        raise
    except OSError:
        raise SessionStoreError(f"could not open session lock: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _lock_stream(stream: BinaryIO, *, nonblocking: bool) -> None:
    try:
        if os.name == "nt":
            stream.seek(0)
            mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
            msvcrt.locking(stream.fileno(), mode, 1)
        else:
            operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(stream.fileno(), operation)
    except (OSError, BlockingIOError):
        if nonblocking:
            raise SessionLockedError("session already has an active writer") from None
        raise SessionStoreError("could not lock session directory") from None


def _unlock_stream(stream: BinaryIO) -> None:
    try:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _release_active_writer(path: Path) -> None:
    with _ACTIVE_WRITERS_GUARD:
        _ACTIVE_WRITERS.discard(str(path))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        raise SessionStoreError(f"could not fsync session directory: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _info(path: Path, state: ReplayState) -> SessionInfo:
    return SessionInfo(
        session_id=state.header.session_id,
        path=path,
        workspace=state.header.workspace,
        workspace_fingerprint=state.header.workspace_fingerprint,
        created_at=state.header.created_at,
        record_count=len(state.records),
        turn_count=len(state.turns),
        closed=state.closed,
        binding=state.binding,
    )
