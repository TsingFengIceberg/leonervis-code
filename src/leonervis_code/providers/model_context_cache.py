"""Private persistent cache for successful model-context discovery."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from threading import RLock

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from leonervis_code.providers.model_context import (
    MAX_CONTEXT_WINDOW_TOKENS,
    MAX_MODEL_OUTPUT_TOKENS,
    ModelContextCapability,
    ModelContextSource,
    ModelContextTarget,
)

CACHE_SCHEMA_VERSION = 2
DEFAULT_CACHE_TTL = timedelta(hours=24)
MAX_CACHE_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 512
MAX_IDENTITY_LENGTH = 2048


def default_model_context_cache_path(environment: dict[str, str] | None = None) -> Path:
    """Return the XDG-aware derived capability cache path."""
    env = os.environ if environment is None else environment
    configured = env.get("XDG_CACHE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "leonervis-code" / "model-context-capabilities.json"


class ModelContextCapabilityCache:
    """Store only bounded, non-secret, positive discovery observations."""

    def __init__(
        self,
        path: Path,
        *,
        ttl: timedelta = DEFAULT_CACHE_TTL,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("model context cache TTL must be positive")
        self.path = Path(path)
        self.ttl = ttl
        self._thread_lock = RLock()

    def get(
        self, target: ModelContextTarget, *, now: datetime
    ) -> tuple[ModelContextCapability | None, str | None]:
        try:
            with self._locked():
                entries = self._read()
        except _CacheError:
            return None, "model context cache was unavailable or unsafe"
        entry = entries.get(_target_key(target))
        if entry is None or entry["target"] != _target_dict(target):
            return None, None
        fetched_at = _parse_time(entry["fetched_at"])
        expires_at = fetched_at + self.ttl
        normalized_now = _normalized_time(now)
        if normalized_now >= expires_at:
            return None, None
        return (
            ModelContextCapability(
                target=target,
                context_window_tokens=entry.get("context_window_tokens"),
                source=(
                    ModelContextSource.DISCOVERY_CACHE
                    if entry.get("context_window_tokens") is not None
                    else ModelContextSource.UNKNOWN
                ),
                discovered_at=(
                    _format_time(fetched_at)
                    if entry.get("context_window_tokens") is not None
                    else None
                ),
                expires_at=(
                    _format_time(expires_at)
                    if entry.get("context_window_tokens") is not None
                    else None
                ),
                model_max_output_tokens=entry.get("model_max_output_tokens"),
                model_max_output_source=(
                    ModelContextSource.DISCOVERY_CACHE
                    if entry.get("model_max_output_tokens") is not None
                    else ModelContextSource.UNKNOWN
                ),
                model_max_output_discovered_at=(
                    _format_time(fetched_at)
                    if entry.get("model_max_output_tokens") is not None
                    else None
                ),
                model_max_output_expires_at=(
                    _format_time(expires_at)
                    if entry.get("model_max_output_tokens") is not None
                    else None
                ),
            ),
            None,
        )

    def put(
        self,
        target: ModelContextTarget,
        context_window_tokens: int | None,
        model_max_output_tokens: int | None = None,
        *,
        now: datetime,
    ) -> str | None:
        context_valid = context_window_tokens is None or (
            type(context_window_tokens) is int
            and 1 <= context_window_tokens <= MAX_CONTEXT_WINDOW_TOKENS
        )
        output_valid = model_max_output_tokens is None or (
            type(model_max_output_tokens) is int
            and 1 <= model_max_output_tokens <= MAX_MODEL_OUTPUT_TOKENS
        )
        if (
            not context_valid
            or not output_valid
            or (context_window_tokens is None and model_max_output_tokens is None)
        ):
            return "model context cache rejected an invalid limit"
        try:
            with self._locked():
                entries = self._read()
                key = _target_key(target)
                entries[key] = {
                    "target": _target_dict(target),
                    "context_window_tokens": context_window_tokens,
                    "model_max_output_tokens": model_max_output_tokens,
                    "fetched_at": _format_time(_normalized_time(now)),
                }
                if len(entries) > MAX_CACHE_ENTRIES:
                    ordered = sorted(
                        entries.items(), key=lambda item: item[1]["fetched_at"], reverse=True
                    )
                    entries = dict(ordered[:MAX_CACHE_ENTRIES])
                self._write(entries)
        except _CacheError:
            return "model context cache could not persist the live result"
        return None

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            stream = _open_lock_file(self.path.parent / ".model-context.lock")
            try:
                _lock_stream(stream)
                yield
            finally:
                _unlock_stream(stream)
                stream.close()

    def _read(self) -> dict[str, dict[str, object]]:
        path = self.path
        if path.is_symlink():
            raise _CacheError
        if not path.exists():
            return {}
        _ensure_safe_existing_chain(path)
        _ensure_safe_path(path, require_file=True)
        try:
            if path.stat().st_size > MAX_CACHE_BYTES:
                raise _CacheError
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise _CacheError from None
        if not isinstance(data, dict) or set(data) != {"schema_version", "entries"}:
            raise _CacheError
        if data["schema_version"] != CACHE_SCHEMA_VERSION:
            raise _CacheError
        raw_entries = data["entries"]
        if not isinstance(raw_entries, dict) or len(raw_entries) > MAX_CACHE_ENTRIES:
            raise _CacheError
        entries: dict[str, dict[str, object]] = {}
        for key, entry in raw_entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                raise _CacheError
            if set(entry) != {
                "target",
                "context_window_tokens",
                "model_max_output_tokens",
                "fetched_at",
            }:
                raise _CacheError
            target = entry["target"]
            if not isinstance(target, dict) or set(target) != {
                "provider_id",
                "protocol",
                "base_url",
                "wire_model",
                "credential_env",
            }:
                raise _CacheError
            for field in ("provider_id", "protocol", "base_url", "wire_model"):
                value = target[field]
                if not isinstance(value, str) or not value or len(value) > MAX_IDENTITY_LENGTH:
                    raise _CacheError
            credential_env = target["credential_env"]
            if credential_env is not None and (
                not isinstance(credential_env, str)
                or not credential_env
                or len(credential_env) > MAX_IDENTITY_LENGTH
            ):
                raise _CacheError
            tokens = entry["context_window_tokens"]
            if tokens is not None and (
                type(tokens) is not int or not 1 <= tokens <= MAX_CONTEXT_WINDOW_TOKENS
            ):
                raise _CacheError
            output_tokens = entry["model_max_output_tokens"]
            if output_tokens is not None and (
                type(output_tokens) is not int or not 1 <= output_tokens <= MAX_MODEL_OUTPUT_TOKENS
            ):
                raise _CacheError
            if tokens is None and output_tokens is None:
                raise _CacheError
            fetched_at = entry["fetched_at"]
            if not isinstance(fetched_at, str):
                raise _CacheError
            _parse_time(fetched_at)
            if key != _target_key_from_mapping(target):
                raise _CacheError
            entries[key] = entry
        return entries

    def _write(self, entries: dict[str, dict[str, object]]) -> None:
        path = self.path
        parent = path.parent
        _ensure_safe_parent_chain(parent)
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        except OSError:
            raise _CacheError from None
        _ensure_safe_path(parent, require_file=False)
        if path.exists() or path.is_symlink():
            _ensure_safe_path(path, require_file=True)
        payload = (
            json.dumps(
                {"schema_version": CACHE_SCHEMA_VERSION, "entries": entries},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )
        if len(payload.encode("utf-8")) > MAX_CACHE_BYTES:
            raise _CacheError
        descriptor: int | None = None
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
            _fsync_directory(parent)
        except OSError:
            raise _CacheError from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass


class _CacheError(Exception):
    pass


def _target_dict(target: ModelContextTarget) -> dict[str, object]:
    return {
        "provider_id": target.provider_id,
        "protocol": target.protocol.value,
        "base_url": target.base_url,
        "wire_model": target.wire_model,
        "credential_env": target.credential_env,
    }


def _target_key(target: ModelContextTarget) -> str:
    return _target_key_from_mapping(_target_dict(target))


def _target_key_from_mapping(value: dict[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _normalized_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _CacheError from None
    if parsed.tzinfo is None:
        raise _CacheError
    return parsed.astimezone(UTC)


def _open_lock_file(path: Path):
    _ensure_safe_parent_chain(path.parent)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError:
        raise _CacheError from None
    _ensure_safe_path(path.parent, require_file=False)
    if path.is_symlink():
        raise _CacheError
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "a+b")
    except OSError:
        raise _CacheError from None


def _lock_stream(stream) -> None:
    try:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    except OSError:
        raise _CacheError from None


def _unlock_stream(stream) -> None:
    try:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _ensure_safe_existing_chain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.parts[0])
    for part in absolute.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink():
            raise _CacheError


def _ensure_safe_parent_chain(path: Path) -> None:
    _ensure_safe_existing_chain(path)
    current = path
    while not current.exists() and not current.is_symlink():
        if current == current.parent:
            break
        current = current.parent
    if current.is_symlink() or (current.exists() and not current.is_dir()):
        raise _CacheError


def _ensure_safe_path(path: Path, *, require_file: bool) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise _CacheError from None
    if stat.S_ISLNK(info.st_mode):
        raise _CacheError
    expected = stat.S_ISREG(info.st_mode) if require_file else stat.S_ISDIR(info.st_mode)
    if not expected:
        raise _CacheError


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
