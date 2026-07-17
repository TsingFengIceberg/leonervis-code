"""Versioned storage for user provider profiles and workspace active selection."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import stat
import tempfile
from threading import RLock
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from leonervis_code.providers.profile import (
    NamedProviderProfile,
    ProviderProfileError,
    ProviderProfileSpec,
    legacy_profile_id,
)

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}
MAX_CONFIGURATION_BYTES = 1024 * 1024
MAX_PROFILES = 256


@dataclass(frozen=True)
class ActiveProfileSelection:
    """One active profile identity and the configuration layer that selected it."""

    name: str
    source: str
    profile_id: str
    revision: int


@dataclass(frozen=True)
class _UserState:
    schema_version: int
    profiles: dict[str, NamedProviderProfile]
    active_profile_id: str | None


@dataclass(frozen=True)
class _ProjectState:
    schema_version: int
    active_profile_id: str | None = None
    legacy_active_name: str | None = None


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
    """Read v1/v2 configurations and write schema v2 with per-file atomic replacement."""

    def __init__(self, user_path: Path, project_path: Path) -> None:
        self.user_path = Path(user_path)
        self.project_path = Path(project_path)
        self._thread_lock = RLock()
        self._transaction_depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize one operation across threads and processes; not a cross-file ACID claim."""
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
        """Return every user profile ordered by its current name."""
        state = self._load_user()
        return tuple(sorted(state.profiles.values(), key=lambda profile: profile.name))

    def get_profile(self, name: str) -> NamedProviderProfile:
        """Return one profile by exact, case-sensitive name."""
        matches = [
            profile for profile in self._load_user().profiles.values() if profile.name == name
        ]
        if not matches:
            raise ProviderProfileError(f"provider profile does not exist: {name}")
        return matches[0]

    def get_profile_by_id(self, profile_id: str) -> NamedProviderProfile:
        """Return one profile by canonical persistent identity."""
        try:
            return self._load_user().profiles[profile_id]
        except KeyError:
            raise ProviderProfileError(
                f"provider profile ID does not exist: {profile_id}"
            ) from None

    def add_profile(
        self,
        profile: ProviderProfileSpec,
        *,
        replace: bool = False,
        expected_revision: int | None = None,
    ) -> NamedProviderProfile:
        """Create a UUID4 profile or replace the same named identity with revision CAS."""
        spec = _as_spec(profile)
        with self.transaction():
            state = self._load_user()
            existing = _profile_named(state, spec.name)
            if existing is None:
                if replace and expected_revision is not None:
                    raise ProviderProfileError(f"provider profile does not exist: {spec.name}")
                if len(state.profiles) >= MAX_PROFILES:
                    raise ProviderProfileError(f"provider profile limit reached ({MAX_PROFILES})")
                created = _owned_profile(spec, profile_id=str(uuid4()), revision=1)
                profiles = {**state.profiles, created.profile_id: created}
                self._write_user(profiles, state.active_profile_id)
                return created
            if not replace:
                raise ProviderProfileError(
                    f"provider profile already exists: {spec.name}; use --replace to update it"
                )
            _check_revision(existing, expected_revision)
            if _same_spec(existing, spec):
                return existing
            updated = _owned_profile(
                spec, profile_id=existing.profile_id, revision=existing.revision + 1
            )
            profiles = {**state.profiles, existing.profile_id: updated}
            self._write_user(profiles, state.active_profile_id)
            return updated

    def replace_profile(
        self,
        profile_id: str,
        spec: ProviderProfileSpec,
        *,
        expected_revision: int | None = None,
    ) -> NamedProviderProfile:
        """Replace one profile by identity, preserving ID and incrementing on real change."""
        spec = _as_spec(spec)
        with self.transaction():
            state = self._load_user()
            existing = _profile_by_id(state, profile_id)
            _check_revision(existing, expected_revision)
            collision = _profile_named(state, spec.name)
            if collision is not None and collision.profile_id != profile_id:
                raise ProviderProfileError(f"provider profile already exists: {spec.name}")
            if _same_spec(existing, spec):
                return existing
            updated = _owned_profile(
                spec, profile_id=existing.profile_id, revision=existing.revision + 1
            )
            profiles = {**state.profiles, profile_id: updated}
            self._write_user(profiles, state.active_profile_id)
            return updated

    def rename_profile(
        self,
        profile_id: str,
        name: str,
        *,
        expected_revision: int | None = None,
    ) -> NamedProviderProfile:
        """Rename one identity without invalidating legacy ID-based project selection."""
        with self.transaction():
            state = self._load_user()
            existing = _profile_by_id(state, profile_id)
            _check_revision(existing, expected_revision)
            if existing.name == name:
                return existing
            collision = _profile_named(state, name)
            if collision is not None:
                raise ProviderProfileError(f"provider profile already exists: {name}")
            renamed_spec = replace(existing.to_spec(), name=name)
            renamed = _owned_profile(
                renamed_spec,
                profile_id=existing.profile_id,
                revision=existing.revision + 1,
            )
            profiles = {**state.profiles, profile_id: renamed}
            self._write_user(profiles, state.active_profile_id)
            return renamed

    def remove_profile(
        self,
        name: str,
        *,
        expected_revision: int | None = None,
    ) -> None:
        """Remove one inactive profile by name; re-adding the name receives a new ID."""
        profile = self.get_profile(name)
        self.remove_profile_by_id(profile.profile_id, expected_revision=expected_revision)

    def remove_profile_by_id(
        self,
        profile_id: str,
        *,
        expected_revision: int | None = None,
    ) -> None:
        """Remove one inactive profile by identity."""
        with self.transaction():
            state = self._load_user()
            profile = _profile_by_id(state, profile_id)
            _check_revision(profile, expected_revision)
            project = self._load_project()
            project_active = self._resolve_project_id(project, state)
            if (
                state.active_profile_id == profile.profile_id
                or project_active == profile.profile_id
            ):
                raise ProviderProfileError(
                    f"provider profile is active: {profile.name}; "
                    "clear or change the active profile first"
                )
            profiles = dict(state.profiles)
            del profiles[profile.profile_id]
            self._write_user(profiles, state.active_profile_id)

    def active_selection(self) -> ActiveProfileSelection | None:
        """Resolve project-over-user active selection across all v1/v2 combinations."""
        user = self._load_user()
        project = self._load_project()
        project_id = self._resolve_project_id(project, user)
        if project_id is not None:
            return _selection(_profile_by_id(user, project_id, layer="project"), "project")
        if user.active_profile_id is not None:
            return _selection(_profile_by_id(user, user.active_profile_id, layer="user"), "user")
        return None

    def active_name(self, scope: str) -> str | None:
        """Return the current name selected at exactly one scope."""
        self._validate_scope(scope)
        user = self._load_user()
        if scope == "project":
            profile_id = self._resolve_project_id(self._load_project(), user)
        else:
            profile_id = user.active_profile_id
        return _profile_by_id(user, profile_id).name if profile_id is not None else None

    def selection_with(self, name: str, scope: str) -> ActiveProfileSelection:
        """Preview effective selection after selecting a profile by name, without writing."""
        profile = self.get_profile(name)
        return self.selection_with_id(profile.profile_id, scope)

    def selection_with_id(self, profile_id: str, scope: str) -> ActiveProfileSelection:
        """Preview effective selection after selecting an identity, without writing."""
        self._validate_scope(scope)
        user = self._load_user()
        selected = _profile_by_id(user, profile_id)
        project_id = self._resolve_project_id(self._load_project(), user)
        user_id = user.active_profile_id
        if scope == "project":
            project_id = selected.profile_id
        else:
            user_id = selected.profile_id
        if project_id is not None:
            return _selection(_profile_by_id(user, project_id, layer="project"), "project")
        assert user_id is not None
        return _selection(_profile_by_id(user, user_id, layer="user"), "user")

    def selection_without(self, scope: str) -> ActiveProfileSelection | None:
        """Preview effective selection after clearing one layer, without writing."""
        self._validate_scope(scope)
        user = self._load_user()
        project_id = self._resolve_project_id(self._load_project(), user)
        user_id = user.active_profile_id
        if scope == "project":
            project_id = None
        else:
            user_id = None
        if project_id is not None:
            return _selection(_profile_by_id(user, project_id, layer="project"), "project")
        if user_id is not None:
            return _selection(_profile_by_id(user, user_id, layer="user"), "user")
        return None

    def set_active(self, name: str, *, scope: str) -> None:
        """Persist an existing profile selected by current name."""
        profile = self.get_profile(name)
        self.set_active_id(profile.profile_id, scope=scope)

    def set_active_id(self, profile_id: str, *, scope: str) -> None:
        """Persist an existing profile identity at one scope."""
        with self.transaction():
            self._validate_scope(scope)
            user = self._load_user()
            profile = _profile_by_id(user, profile_id)
            if scope == "project":
                self._write_project(profile.profile_id)
            else:
                self._write_user(user.profiles, profile.profile_id)

    def clear_active(self, *, scope: str) -> None:
        """Clear one active layer and upgrade only the file written by this operation."""
        with self.transaction():
            self._validate_scope(scope)
            if scope == "project":
                self._write_project(None)
            else:
                user = self._load_user()
                self._write_user(user.profiles, None)

    def migrate(self) -> None:
        """Explicitly rewrite readable v1 files as v2; each file is independently atomic."""
        with self.transaction():
            user = self._load_user()
            project = self._load_project()
            project_id = self._resolve_project_id(project, user)
            if user.schema_version == 1:
                self._write_user(user.profiles, user.active_profile_id)
            if project.schema_version == 1:
                self._write_project(project_id)

    def _load_user(self) -> _UserState:
        data = self._read_json(self.user_path)
        if data is None:
            return _UserState(SCHEMA_VERSION, {}, None)
        version = self._schema_version(data, "user")
        if version == 1:
            self._validate_fields(data, {"schema_version", "active_profile", "profiles"}, "user")
            active_name = self._optional_text(data.get("active_profile"), "user active profile")
            raw_profiles = data.get("profiles")
            if not isinstance(raw_profiles, dict):
                raise ProviderProfileError("user provider profiles must be a JSON object")
            if len(raw_profiles) > MAX_PROFILES:
                raise ProviderProfileError(f"provider profile limit exceeded ({MAX_PROFILES})")
            profiles: dict[str, NamedProviderProfile] = {}
            for name, raw_profile in raw_profiles.items():
                if not isinstance(name, str) or not isinstance(raw_profile, dict):
                    raise ProviderProfileError("user provider profile entries are malformed")
                spec = ProviderProfileSpec.from_mapping(raw_profile)
                if spec.name != name:
                    raise ProviderProfileError(f"provider profile key/name mismatch: {name}")
                profile_id = legacy_profile_id(name)
                profiles[profile_id] = _owned_profile(spec, profile_id=profile_id, revision=1)
            active_id = legacy_profile_id(active_name) if active_name is not None else None
            if active_id is not None and active_id not in profiles:
                raise ProviderProfileError(
                    f"user active provider profile does not exist: {active_name}"
                )
            return _UserState(version, profiles, active_id)

        self._validate_fields(data, {"schema_version", "active_profile_id", "profiles"}, "user")
        active_id = self._optional_text(data.get("active_profile_id"), "user active profile ID")
        raw_profiles = data.get("profiles")
        if not isinstance(raw_profiles, dict):
            raise ProviderProfileError("user provider profiles must be a JSON object")
        if len(raw_profiles) > MAX_PROFILES:
            raise ProviderProfileError(f"provider profile limit exceeded ({MAX_PROFILES})")
        profiles = {}
        names: set[str] = set()
        for profile_id, raw_profile in raw_profiles.items():
            if not isinstance(profile_id, str) or not isinstance(raw_profile, dict):
                raise ProviderProfileError("user provider profile entries are malformed")
            missing_identity = {"profile_id", "revision"} - set(raw_profile)
            if missing_identity:
                raise ProviderProfileError(
                    f"schema-v2 profile is missing required field: {sorted(missing_identity)[0]}"
                )
            profile = NamedProviderProfile.from_mapping(raw_profile)
            if profile.profile_id != profile_id:
                raise ProviderProfileError(f"provider profile key/ID mismatch: {profile_id}")
            if profile.name in names:
                raise ProviderProfileError(f"duplicate provider profile name: {profile.name}")
            names.add(profile.name)
            profiles[profile_id] = profile
        if active_id is not None and active_id not in profiles:
            raise ProviderProfileError(
                f"user active provider profile ID does not exist: {active_id}"
            )
        return _UserState(version, profiles, active_id)

    def _load_project(self) -> _ProjectState:
        data = self._read_json(self.project_path)
        if data is None:
            return _ProjectState(SCHEMA_VERSION)
        version = self._schema_version(data, "project")
        if version == 1:
            self._validate_fields(data, {"schema_version", "active_profile"}, "project")
            return _ProjectState(
                version,
                legacy_active_name=self._optional_text(
                    data.get("active_profile"), "project active profile"
                ),
            )
        self._validate_fields(data, {"schema_version", "active_profile_id"}, "project")
        return _ProjectState(
            version,
            active_profile_id=self._optional_text(
                data.get("active_profile_id"), "project active profile ID"
            ),
        )

    @staticmethod
    def _resolve_project_id(project: _ProjectState, user: _UserState) -> str | None:
        if project.schema_version == 2:
            profile_id = project.active_profile_id
        elif project.legacy_active_name is None:
            profile_id = None
        else:
            named = _profile_named(user, project.legacy_active_name)
            profile_id = (
                named.profile_id
                if named is not None
                else legacy_profile_id(project.legacy_active_name)
            )
        if profile_id is not None and profile_id not in user.profiles:
            label = project.legacy_active_name or profile_id
            raise ProviderProfileError(f"project active provider profile does not exist: {label}")
        return profile_id

    def _write_user(
        self, profiles: Mapping[str, NamedProviderProfile], active_profile_id: str | None
    ) -> None:
        self._atomic_write(
            self.user_path,
            {
                "schema_version": SCHEMA_VERSION,
                "active_profile_id": active_profile_id,
                "profiles": {
                    profile_id: profiles[profile_id].to_dict() for profile_id in sorted(profiles)
                },
            },
        )

    def _write_project(self, active_profile_id: str | None) -> None:
        self._atomic_write(
            self.project_path,
            {"schema_version": SCHEMA_VERSION, "active_profile_id": active_profile_id},
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

    @staticmethod
    def _schema_version(data: dict[str, object], label: str) -> int:
        if "schema_version" not in data:
            raise ProviderProfileError(f"{label} provider configuration is missing schema_version")
        version = data["schema_version"]
        if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ProviderProfileError(f"unsupported {label} provider configuration schema version")
        return version

    @staticmethod
    def _optional_text(value: object, label: str) -> str | None:
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
            _fsync_directory(parent)
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


def _as_spec(profile: ProviderProfileSpec) -> ProviderProfileSpec:
    if not isinstance(profile, ProviderProfileSpec):
        raise ProviderProfileError("profile must be a ProviderProfileSpec")
    return profile.to_spec() if isinstance(profile, NamedProviderProfile) else profile


def _owned_profile(
    spec: ProviderProfileSpec, *, profile_id: str, revision: int
) -> NamedProviderProfile:
    return NamedProviderProfile(
        name=spec.name,
        provider_id=spec.provider_id,
        protocol=spec.protocol,
        model=spec.model,
        base_url=spec.base_url,
        api_key_env=spec.api_key_env,
        max_output_tokens=spec.max_output_tokens,
        temperature=spec.temperature,
        profile_id=profile_id,
        revision=revision,
    )


def _profile_named(state: _UserState, name: str) -> NamedProviderProfile | None:
    return next((profile for profile in state.profiles.values() if profile.name == name), None)


def _profile_by_id(
    state: _UserState, profile_id: str, *, layer: str | None = None
) -> NamedProviderProfile:
    try:
        return state.profiles[profile_id]
    except KeyError:
        prefix = f"{layer} active " if layer else ""
        raise ProviderProfileError(
            f"{prefix}provider profile ID does not exist: {profile_id}"
        ) from None


def _selection(profile: NamedProviderProfile, source: str) -> ActiveProfileSelection:
    return ActiveProfileSelection(
        name=profile.name,
        source=source,
        profile_id=profile.profile_id,
        revision=profile.revision,
    )


def _same_spec(profile: NamedProviderProfile, spec: ProviderProfileSpec) -> bool:
    return profile.to_spec() == spec


def _check_revision(profile: NamedProviderProfile, expected_revision: int | None) -> None:
    if expected_revision is None:
        return
    if type(expected_revision) is not int or expected_revision < 1:
        raise ProviderProfileError("expected profile revision must be a positive integer")
    if profile.revision != expected_revision:
        raise ProviderProfileError(
            f"provider profile revision conflict: expected {expected_revision}, "
            f"found {profile.revision}"
        )


def _fsync_directory(path: Path) -> None:
    """Best-effort durability for the rename; unsupported platforms may reject directory fsync."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
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
