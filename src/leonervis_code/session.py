"""Project-facing persistent conversation facade for the local provider runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.core.contracts import ConversationItem, ConversationProvider, ConversationTurn
from leonervis_code.providers.manager import RuntimeProviderManager, RuntimeStatus
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.tools.read_file import ReadFileTool


class ProjectSession:
    """Keep one workspace, conversation history, and provider runtime together."""

    def __init__(
        self,
        workspace: Path,
        store: ProviderProfileStore,
        manager: RuntimeProviderManager,
        loop: AgentLoop,
    ) -> None:
        self.workspace = workspace
        self._store = store
        self._manager = manager
        self._loop = loop
        self._closed = False

    @classmethod
    def open(
        cls,
        workspace: Path,
        *,
        profile: str | None = None,
        model: str | None = None,
        custom_protocol: str | None = None,
        custom_base_url: str | None = None,
        custom_api_key_env: str | None = None,
        environment: Mapping[str, str] | None = None,
        user_profile_path: Path | None = None,
        project_profile_path: Path | None = None,
        provider_factory: Callable[..., ConversationProvider] | None = None,
        read_file_factory: Callable[[Path], ReadFileTool] = ReadFileTool,
    ) -> ProjectSession:
        """Open one long-lived local session using the unified selection precedence."""
        resolved_workspace = Path(workspace).resolve()
        resolved_environment = environment if environment is not None else os.environ
        store = ProviderProfileStore.for_workspace(
            resolved_workspace,
            environment=resolved_environment,
            user_path=user_profile_path,
            project_path=project_profile_path,
        )
        manager_arguments: dict[str, object] = {
            "environment": resolved_environment,
            "profile": profile,
            "model": model,
            "custom_protocol": custom_protocol,
            "custom_base_url": custom_base_url,
            "custom_api_key_env": custom_api_key_env,
        }
        if provider_factory is not None:
            manager_arguments["provider_factory"] = provider_factory
        manager = RuntimeProviderManager(store, **manager_arguments)  # type: ignore[arg-type]
        try:
            loop = AgentLoop(None, read_file_factory(resolved_workspace))
        except Exception:
            manager.close()
            raise
        return cls(resolved_workspace, store, manager, loop)

    @property
    def history(self) -> tuple[ConversationItem, ...]:
        """Return all committed neutral conversation items."""
        return self._loop.history

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        """Return all committed user-visible conversation turns."""
        return self._loop.turns

    def prompt(self, text: str) -> str:
        """Run one complete turn with the current provider pinned throughout."""
        self._ensure_open()
        with self._manager.provider_for_turn() as provider:
            return self._loop.run(text, provider=provider)

    def list_profiles(self) -> tuple[NamedProviderProfile, ...]:
        """Return every globally registered named provider profile."""
        self._ensure_open()
        return self._store.list_profiles()

    def use_profile(self, name: str, *, scope: str = "project") -> RuntimeStatus:
        """Switch to and persist one named provider between turns."""
        self._ensure_open()
        return self._manager.use_profile(name, scope=scope)

    def clear_active(self, *, scope: str = "project") -> RuntimeStatus:
        """Clear one persisted profile selection and activate the next layer."""
        self._ensure_open()
        return self._manager.clear_active(scope=scope)

    def set_model(self, model: str) -> RuntimeStatus:
        """Apply a runtime-only model override to the active profile."""
        self._ensure_open()
        return self._manager.set_model(model)

    def status(self) -> RuntimeStatus:
        """Return the redacted active runtime status."""
        self._ensure_open()
        return self._manager.status()

    def close(self) -> None:
        """Close this session and its provider client once."""
        if self._closed:
            return
        self._manager.close()
        self._closed = True

    def __enter__(self) -> ProjectSession:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("project session is closed")
