"""Atomic storage for global provider profiles and workspace active selection."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
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

from leonervis_code.providers.profile import NamedProviderProfile, ProviderProfileError

SCHEMA_VERSION = 1
MAX_CONFIGURATION_BYTES = 1024 * 1024
MAX_PROFILES = 256


@dataclass(frozen=True)
class ActiveProfileSelection:
    """One active profile and the configuration layer that selected it."""

    name: str
    source: str


def default_user_profile_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the XDG-aware per-user provider registry path."""
    env = os.environ if environment is None else environment
    configured = env.get("XDG_CONFIG_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    return root / "leonervis-code" / "providers.json"


def default_project_profile_path(workspace: Path) -> Path:
    """Return the workspace-local active-profile override path."""
    return workspace / ".leonervis-code" / "provider.json"


class ProviderProfileStore:
    """Read and atomically update named profiles without credential values."""

    def __init__(self, user_path: Path, project_path: Path) -> None:
        self.user_path = Path(user_path)
        self.project_path = Path(project_path)
        self._thread_lock = RLock()
        self._transaction_depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize a profile read/validate/write transaction across threads and processes."""
        with self._thread_lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            lock_paths = sorted(
                {
                    self.user_path.parent / ".providers.lock",
                    self.project_path.parent / ".providers.lock",
                },
                key=str,
            )
            streams = []
            try:
                for path in lock_paths:
                    stream = _open_lock_file(path)
                    _lock_stream(stream)
                    streams.append(stream)
                self._transaction_depth = 1
                yield
            finally:
                self._transaction_depth = 0
                for stream in reversed(streams):
                    try:
                        _unlock_stream(stream)
                    finally:
                        stream.close()

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        environment: Mapping[str, str] | None = None,
        user_path: Path | None = None,
        project_path: Path | None = None,
    ) -> ProviderProfileStore:
        """Create a store using default paths unless explicit test paths are supplied."""
        return cls(
            user_path or default_user_profile_path(environment),
            project_path or default_project_profile_path(workspace),
        )

    def list_profiles(self) -> tuple[NamedProviderProfile, ...]:
        """Return every user profile ordered by name."""
        profiles, _ = self._load_user()
        return tuple(profiles[name] for name in sorted(profiles))

    def get_profile(self, name: str) -> NamedProviderProfile:
        """Return one named user profile or raise a safe configuration error."""
        profiles, _ = self._load_user()
        try:
            return profiles[name]
        except KeyError:
            raise ProviderProfileError(f"provider profile does not exist: {name}") from None

    def add_profile(self, profile: NamedProviderProfile, *, replace: bool = False) -> None:
        """Create or explicitly replace a profile in the user registry."""
        with self.transaction():
            profiles, active = self._load_user()
            if profile.name in profiles and not replace:
                raise ProviderProfileError(
                    f"provider profile already exists: {profile.name}; use --replace to update it"
                )
            if profile.name not in profiles and len(profiles) >= MAX_PROFILES:
                raise ProviderProfileError(f"provider profile limit reached ({MAX_PROFILES})")
            profiles[profile.name] = profile
            self._write_user(profiles, active)

    def remove_profile(self, name: str) -> None:
        """Remove a profile not active in the current known configuration layers."""
        with self.transaction():
            profiles, user_active = self._load_user()
            project_active = self._load_project()
            if name not in profiles:
                raise ProviderProfileError(f"provider profile does not exist: {name}")
            if user_active == name or project_active == name:
                raise ProviderProfileError(
                    f"provider profile is active: {name}; clear or change the active profile first"
                )
            del profiles[name]
            self._write_user(profiles, user_active)

    def active_selection(self) -> ActiveProfileSelection | None:
        """Resolve project-over-user active selection and reject dangling references."""
        profiles, user_active = self._load_user()
        project_active = self._load_project()
        if project_active is not None:
            if project_active not in profiles:
                raise ProviderProfileError(
                    f"project active provider profile does not exist: {project_active}"
                )
            return ActiveProfileSelection(project_active, "project")
        if user_active is not None:
            if user_active not in profiles:
                raise ProviderProfileError(
                    f"user active provider profile does not exist: {user_active}"
                )
            return ActiveProfileSelection(user_active, "user")
        return None

    def active_name(self, scope: str) -> str | None:
        """Return the active name at exactly one configuration scope."""
        self._validate_scope(scope)
        if scope == "project":
            return self._load_project()
        return self._load_user()[1]

    def selection_with(self, name: str, scope: str) -> ActiveProfileSelection:
        """Preview the effective selection after setting one layer, without writing."""
        self._validate_scope(scope)
        profiles, user_active = self._load_user()
        project_active = self._load_project()
        if name not in profiles:
            raise ProviderProfileError(f"provider profile does not exist: {name}")
        if scope == "project":
            project_active = name
        else:
            user_active = name
        if project_active is not None:
            if project_active not in profiles:
                raise ProviderProfileError(
                    f"project active provider profile does not exist: {project_active}"
                )
            return ActiveProfileSelection(project_active, "project")
        assert user_active is not None
        return ActiveProfileSelection(user_active, "user")

    def selection_without(self, scope: str) -> ActiveProfileSelection | None:
        """Preview the effective selection after clearing one layer, without writing."""
        self._validate_scope(scope)
        profiles, user_active = self._load_user()
        project_active = self._load_project()
        if scope == "project":
            project_active = None
        else:
            user_active = None
        if project_active is not None:
            if project_active not in profiles:
                raise ProviderProfileError(
                    f"project active provider profile does not exist: {project_active}"
                )
            return ActiveProfileSelection(project_active, "project")
        if user_active is not None:
            if user_active not in profiles:
                raise ProviderProfileError(
                    f"user active provider profile does not exist: {user_active}"
                )
            return ActiveProfileSelection(user_active, "user")
        return None

    def set_active(self, name: str, *, scope: str) -> None:
        """Persist an existing profile as active at one scope."""
        with self.transaction():
            self._validate_scope(scope)
            profiles, _ = self._load_user()
            if name not in profiles:
                raise ProviderProfileError(f"provider profile does not exist: {name}")
            if scope == "project":
                self._write_project(name)
            else:
                self._write_user(profiles, name)

    def clear_active(self, *, scope: str) -> None:
        """Clear one active layer so the next precedence layer becomes effective."""
        with self.transaction():
            self._validate_scope(scope)
            if scope == "project":
                self._write_project(None)
            else:
                profiles, _ = self._load_user()
                self._write_user(profiles, None)

    def _load_user(self) -> tuple[dict[str, NamedProviderProfile], str | None]:
        data = self._read_json(self.user_path)
        if data is None:
            return {}, None
        self._validate_fields(data, {"schema_version", "active_profile", "profiles"}, "user")
        self._validate_version(data, "user")
        active = self._optional_name(data.get("active_profile"), "user active profile")
        raw_profiles = data.get("profiles")
        if not isinstance(raw_profiles, dict):
            raise ProviderProfileError("user provider profiles must be a JSON object")
        if len(raw_profiles) > MAX_PROFILES:
            raise ProviderProfileError(f"provider profile limit exceeded ({MAX_PROFILES})")
        profiles: dict[str, NamedProviderProfile] = {}
        for name, raw_profile in raw_profiles.items():
            if not isinstance(name, str) or not isinstance(raw_profile, dict):
                raise ProviderProfileError("user provider profile entries are malformed")
            profile = NamedProviderProfile.from_mapping(raw_profile)
            if profile.name != name:
                raise ProviderProfileError(f"provider profile key/name mismatch: {name}")
            profiles[name] = profile
        if active is not None and active not in profiles:
            raise ProviderProfileError(f"user active provider profile does not exist: {active}")
        return profiles, active

    def _load_project(self) -> str | None:
        data = self._read_json(self.project_path)
        if data is None:
            return None
        self._validate_fields(data, {"schema_version", "active_profile"}, "project")
        self._validate_version(data, "project")
        return self._optional_name(data.get("active_profile"), "project active profile")

    def _write_user(self, profiles: Mapping[str, NamedProviderProfile], active: str | None) -> None:
        self._atomic_write(
            self.user_path,
            {
                "schema_version": SCHEMA_VERSION,
                "active_profile": active,
                "profiles": {name: profiles[name].to_dict() for name in sorted(profiles)},
            },
        )

    def _write_project(self, active: str | None) -> None:
        self._atomic_write(
            self.project_path,
            {"schema_version": SCHEMA_VERSION, "active_profile": active},
        )

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in {"user", "project"}:
            raise ProviderProfileError("profile scope must be user or project")

    @staticmethod
    def _validate_fields(data: dict[str, object], allowed: set[str], label: str) -> None:
        unknown = set(data) - allowed
        if unknown:
            raise ProviderProfileError(
                f"{label} provider configuration contains unknown field: {sorted(unknown)[0]}"
            )
        missing = {"schema_version"} - set(data)
        if missing:
            raise ProviderProfileError(f"{label} provider configuration is missing schema_version")

    @staticmethod
    def _validate_version(data: dict[str, object], label: str) -> None:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ProviderProfileError(f"unsupported {label} provider configuration schema version")

    @staticmethod
    def _optional_name(value: object, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ProviderProfileError(f"{label} must be text or null")
        return value

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        if path.is_symlink():
            raise ProviderProfileError(f"provider configuration path must not be a symlink: {path}")
        if not path.exists():
            return None
        _ensure_safe_existing_chain(path)
        _ensure_safe_path(path, require_file=True)
        try:
            if path.stat().st_size > MAX_CONFIGURATION_BYTES:
                raise ProviderProfileError(
                    f"provider configuration exceeds {MAX_CONFIGURATION_BYTES} bytes: {path}"
                )
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except ProviderProfileError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ProviderProfileError(
                f"provider configuration is unreadable or invalid: {path}"
            ) from None
        if not isinstance(data, dict):
            raise ProviderProfileError(f"provider configuration must be a JSON object: {path}")
        return data

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, object]) -> None:
        parent = path.parent
        _ensure_safe_parent_chain(parent)
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise ProviderProfileError(
                f"could not create provider configuration directory: {parent}"
            ) from None
        _ensure_safe_path(parent, require_file=False)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            raise ProviderProfileError(
                f"could not secure provider configuration directory: {parent}"
            ) from None
        if path.exists() or path.is_symlink():
            _ensure_safe_path(path, require_file=True)

        payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if len(payload.encode("utf-8")) > MAX_CONFIGURATION_BYTES:
            raise ProviderProfileError(
                f"provider configuration exceeds {MAX_CONFIGURATION_BYTES} bytes: {path}"
            )
        temporary_name: str | None = None
        descriptor: int | None = None
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
        except OSError:
            raise ProviderProfileError(f"could not write provider configuration: {path}") from None
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


def _open_lock_file(path: Path):
    _ensure_safe_parent_chain(path.parent)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError:
        raise ProviderProfileError(
            f"could not create provider lock directory: {path.parent}"
        ) from None
    _ensure_safe_path(path.parent, require_file=False)
    if path.is_symlink():
        raise ProviderProfileError(f"provider configuration lock must not be a symlink: {path}")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "a+b")
    except OSError:
        raise ProviderProfileError(f"could not open provider configuration lock: {path}") from None


def _lock_stream(stream) -> None:
    try:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    except OSError:
        raise ProviderProfileError("could not lock provider configuration") from None


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
    """Reject every symlink in an existing absolute or relative path chain."""
    absolute = path.absolute()
    parts = absolute.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink():
            raise ProviderProfileError(
                f"provider configuration path must not be a symlink: {current}"
            )


def _ensure_safe_parent_chain(path: Path) -> None:
    """Reject existing symlinks in the complete configuration directory chain."""
    _ensure_safe_existing_chain(path)
    current = path
    while not current.exists() and not current.is_symlink():
        if current == current.parent:
            break
        current = current.parent
    if current.is_symlink():
        raise ProviderProfileError(f"provider configuration path must not be a symlink: {current}")
    if current.exists() and not current.is_dir():
        raise ProviderProfileError(f"provider configuration parent is not a directory: {current}")


def _ensure_safe_path(path: Path, *, require_file: bool) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise ProviderProfileError(f"provider configuration path is inaccessible: {path}") from None
    if stat.S_ISLNK(info.st_mode):
        raise ProviderProfileError(f"provider configuration path must not be a symlink: {path}")
    expected = stat.S_ISREG(info.st_mode) if require_file else stat.S_ISDIR(info.st_mode)
    if not expected:
        kind = "file" if require_file else "directory"
        raise ProviderProfileError(f"provider configuration path is not a regular {kind}: {path}")
