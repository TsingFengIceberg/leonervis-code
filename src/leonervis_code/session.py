"""Project-facing durable conversation facade for one workspace runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
from threading import RLock

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.core.contracts import (
    CommittedTurn,
    ConversationItem,
    ConversationProvider,
    ConversationTurn,
)
from leonervis_code.providers.manager import RuntimeProviderManager, RuntimeStatus
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.session_records import BindingSnapshot
from leonervis_code.session_store import SessionInfo, SessionStore, SessionStoreError, SessionWriter
from leonervis_code.tools.read_file import ReadFileTool


class ProjectSession:
    """Keep one runtime and one switchable durable conversation for a workspace."""

    def __init__(
        self,
        workspace: Path,
        store: ProviderProfileStore,
        manager: RuntimeProviderManager,
        session_store: SessionStore,
        writer: SessionWriter,
        read_file: ReadFileTool,
    ) -> None:
        self.workspace = workspace
        self._store = store
        self._manager = manager
        self._session_store = session_store
        self._writer = writer
        self._read_file = read_file
        self._lock = RLock()
        self._closed = False
        self._loop = self._new_loop(writer)

    @classmethod
    def open(
        cls,
        workspace: Path,
        *,
        resume: str | Path | None = None,
        profile: str | None = None,
        profile_id: str | None = None,
        model: str | None = None,
        custom_protocol: str | None = None,
        custom_base_url: str | None = None,
        custom_api_key_env: str | None = None,
        environment: Mapping[str, str] | None = None,
        user_profile_path: Path | None = None,
        project_profile_path: Path | None = None,
        provider_factory: Callable[..., ConversationProvider] | None = None,
        read_file_factory: Callable[[Path], ReadFileTool] = ReadFileTool,
        session_store_factory: Callable[[Path], SessionStore] = SessionStore,
    ) -> ProjectSession:
        """Create or resume durable history while selecting runtime independently."""
        resolved_workspace = Path(workspace).resolve()
        resolved_environment = environment if environment is not None else os.environ
        store = ProviderProfileStore.for_workspace(
            resolved_workspace,
            environment=resolved_environment,
            user_path=user_profile_path,
            project_path=project_profile_path,
        )
        if profile is not None and profile_id is not None:
            raise ValueError("profile and profile_id cannot be combined")
        selected_profile = profile
        if profile_id is not None:
            selected_profile = store.get_profile_by_id(profile_id).name
        manager_arguments: dict[str, object] = {
            "environment": resolved_environment,
            "profile": selected_profile,
            "model": model,
            "custom_protocol": custom_protocol,
            "custom_base_url": custom_base_url,
            "custom_api_key_env": custom_api_key_env,
        }
        if provider_factory is not None:
            manager_arguments["provider_factory"] = provider_factory
        manager = RuntimeProviderManager(store, **manager_arguments)  # type: ignore[arg-type]
        writer: SessionWriter | None = None
        try:
            read_file = read_file_factory(resolved_workspace)
            session_store = session_store_factory(resolved_workspace)
            binding = binding_from_status(manager.status())
            writer = (
                session_store.open(resume) if resume is not None else session_store.create(binding)
            )
            return cls(resolved_workspace, store, manager, session_store, writer, read_file)
        except Exception:
            if writer is not None:
                writer.release()
            manager.close()
            raise

    @property
    def session_id(self) -> str:
        return self._writer.session_id

    @property
    def transcript_path(self) -> Path:
        return self._writer.path

    @property
    def history(self) -> tuple[ConversationItem, ...]:
        return self._loop.history

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        return self._loop.turns

    def session_info(self) -> SessionInfo:
        self._ensure_open()
        return self._writer.info

    def list_sessions(self) -> tuple[SessionInfo, ...]:
        self._ensure_open()
        return self._session_store.list()

    def new_session(self) -> SessionInfo:
        """Create and atomically select an empty Session without changing runtime."""
        with self._lock:
            self._ensure_open()
            candidate = self._session_store.create(binding_from_status(self._manager.status()))
            loop = self._new_loop(candidate)
            old = self._writer
            self._writer = candidate
            self._loop = loop
            old.release()
            return candidate.info

    def switch_session(self, selector: str | Path) -> SessionInfo:
        """Atomically swap durable history without changing the current runtime client."""
        with self._lock:
            self._ensure_open()
            candidate = self._session_store.open(selector)
            try:
                loop = self._new_loop(candidate)
            except Exception:
                candidate.release()
                raise
            old = self._writer
            self._writer = candidate
            self._loop = loop
            old.release()
            return candidate.info

    def prompt(self, text: str) -> str:
        """Run one complete turn; transcript fsync succeeds before memory commit."""
        with self._lock:
            self._ensure_open()
            binding = binding_from_status(self._manager.status())
            try:
                with self._manager.provider_for_turn() as provider:
                    return self._loop.run(text, provider=provider)
            except Exception as error:
                self._record_failure(binding, error)
                raise

    def list_profiles(self) -> tuple[NamedProviderProfile, ...]:
        self._ensure_open()
        return self._store.list_profiles()

    def use_profile(self, name: str, *, scope: str = "project") -> RuntimeStatus:
        with self._lock:
            self._ensure_open()
            status = self._manager.use_profile(name, scope=scope)
            self._record_runtime_change(status, "provider_profile")
            return status

    def use_profile_id(self, profile_id: str, *, scope: str = "project") -> RuntimeStatus:
        profile = self._store.get_profile_by_id(profile_id)
        return self.use_profile(profile.name, scope=scope)

    def clear_active(self, *, scope: str = "project") -> RuntimeStatus:
        with self._lock:
            self._ensure_open()
            status = self._manager.clear_active(scope=scope)
            self._record_runtime_change(status, "provider_clear")
            return status

    def set_model(self, model: str) -> RuntimeStatus:
        with self._lock:
            self._ensure_open()
            status = self._manager.set_model(model)
            self._record_runtime_change(status, "model_override")
            return status

    def status(self) -> RuntimeStatus:
        self._ensure_open()
        return self._manager.status()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._writer.close()
            finally:
                self._manager.close()

    def __enter__(self) -> ProjectSession:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _new_loop(self, writer: SessionWriter) -> AgentLoop:
        return AgentLoop(
            None,
            self._read_file,
            initial_history=writer.state.history,
            commit_turn=lambda turn: self._commit_turn(writer, turn),
        )

    def _commit_turn(self, writer: SessionWriter, turn: CommittedTurn) -> None:
        if writer is not self._writer:
            raise SessionStoreError("conversation session changed before turn commit")
        writer.append_turn(turn.items, binding=binding_from_status(self._manager.status()))

    def _record_runtime_change(self, status: RuntimeStatus, reason: str) -> None:
        self._writer.runtime_changed(binding_from_status(status), reason=reason)

    def _record_failure(self, binding: BindingSnapshot, error: Exception) -> None:
        try:
            self._writer.turn_failed(
                binding=binding,
                failure_kind=type(error).__name__,
                message=_safe_failure_message(error),
            )
        except SessionStoreError:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("project session is closed")


def binding_from_status(status: RuntimeStatus, *, generation: int = 0) -> BindingSnapshot:
    """Build non-secret per-turn provenance without influencing future runtime selection."""
    if status.mode == "fake":
        return BindingSnapshot.fake(generation=generation, source=status.selection_source)
    if status.route_fingerprint is None:
        raise SessionStoreError("real runtime status is missing its route fingerprint")
    return BindingSnapshot(
        profile_id=status.profile_id,
        profile_revision=status.profile_revision,
        profile_name=status.profile,
        profile_fingerprint=status.profile_fingerprint,
        provider_id=status.provider_id,
        protocol=status.protocol,
        selected_model=status.selected_model,
        wire_model=status.wire_model,
        base_url=status.base_url,
        base_url_source=status.base_url_source,
        source=status.selection_source,
        credential_env=status.credential_env,
        max_output_tokens=status.max_output_tokens,
        temperature=status.temperature,
        generation=generation,
        adapter_version=f"route-contract-v{status.adapter_contract_version}",
        route_fingerprint=status.route_fingerprint,
    )


def _safe_failure_message(error: Exception) -> str:
    if isinstance(error, ProviderAdapterError):
        return error.failure.message[:4096]
    if isinstance(error, SessionStoreError):
        return str(error)[:4096]
    return type(error).__name__
