"""Long-lived provider client management with atomic between-turn switching."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import RLock

from leonervis_code.core.contracts import ConversationProvider
from leonervis_code.providers.definitions import (
    ADAPTER_CONTRACT_VERSION,
    RuntimeProviderRoute,
)
from leonervis_code.providers.factory import create_provider
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.providers.resolver import resolve_profile_route, resolve_runtime_route

ProviderFactory = Callable[..., ConversationProvider]


class RuntimeProviderStateError(RuntimeError):
    """Raised for unsafe provider lifecycle or concurrent-switch operations."""


@dataclass(frozen=True)
class RuntimeStatus:
    """Redacted current provider state and deterministic runtime provenance."""

    mode: str
    profile: str | None
    selection_source: str
    provider_id: str
    protocol: str | None
    selected_model: str | None
    wire_model: str | None
    base_url: str | None
    base_url_source: str | None
    credential_required: bool
    credential_present: bool
    credential_env: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    profile_id: str | None = None
    profile_revision: int | None = None
    profile_fingerprint: str | None = None
    route_fingerprint: str | None = None
    model_override: str | None = None
    adapter_contract_version: int = ADAPTER_CONTRACT_VERSION

    @property
    def profile_name(self) -> str | None:
        """Expose an explicit name while retaining the existing ``profile`` field."""
        return self.profile


class RuntimeProviderManager:
    """Own one reusable client and replace it atomically only between turns."""

    def __init__(
        self,
        store: ProviderProfileStore,
        *,
        environment: Mapping[str, str],
        profile: str | None = None,
        model: str | None = None,
        custom_protocol: str | None = None,
        custom_base_url: str | None = None,
        custom_api_key_env: str | None = None,
        provider_factory: ProviderFactory = create_provider,
        fake_factory: Callable[[], ConversationProvider] = ScriptedFakeProvider,
    ) -> None:
        self._store = store
        self._environment = environment
        self._provider_factory = provider_factory
        self._fake_factory = fake_factory
        self._lock = RLock()
        self._turn_active = False
        self._closed = False
        self._profile_id: str | None = None
        self._loaded_profile: NamedProviderProfile | None = None
        self._model_override: str | None = None
        self._direct_route: RuntimeProviderRoute | None = None
        self._selection_source = "default"
        self._route: RuntimeProviderRoute | None = None

        if profile is not None:
            selected_profile = store.get_profile(profile)
            route = resolve_profile_route(
                selected_profile, environment=environment, model_override=model
            )
            provider_instance = self._construct(route)
            self._load_profile(selected_profile)
            self._model_override = model
            self._selection_source = "cli"
            self._route = route
            self._provider = provider_instance
        elif model is not None:
            route = resolve_runtime_route(
                model,
                environment=environment,
                custom_protocol=custom_protocol,
                custom_base_url=custom_base_url,
                custom_api_key_env=custom_api_key_env,
            )
            self._route = route
            self._direct_route = route
            self._selection_source = "cli"
            self._provider = self._construct(route)
        else:
            active = store.active_selection()
            if active is None:
                self._provider = fake_factory()
            else:
                selected_profile = store.get_profile_by_id(active.profile_id)
                route = resolve_profile_route(selected_profile, environment=environment)
                self._load_profile(selected_profile)
                self._selection_source = active.source
                self._route = route
                self._provider = self._construct(route)

    @classmethod
    def prepare_profile(
        cls,
        store: ProviderProfileStore,
        name: str,
        *,
        scope: str,
        environment: Mapping[str, str],
        provider_factory: ProviderFactory = create_provider,
    ) -> RuntimeStatus:
        """Validate and persist a profile selection without constructing the old runtime."""
        with store.transaction():
            requested = store.get_profile(name)
            selection = store.selection_with_id(requested.profile_id, scope)
            loaded = store.get_profile_by_id(selection.profile_id)
            route = resolve_profile_route(loaded, environment=environment)
            candidate = provider_factory(route, environment=environment)
            try:
                store.set_active_id(requested.profile_id, scope=scope)
            except Exception:
                _close_provider(candidate)
                raise
        _close_provider(candidate)
        return _status_for_route(
            route,
            profile=loaded,
            source=selection.source,
            environment=environment,
            model_override=None,
        )

    @classmethod
    def prepare_clear(
        cls,
        store: ProviderProfileStore,
        *,
        scope: str,
        environment: Mapping[str, str],
        provider_factory: ProviderFactory = create_provider,
    ) -> RuntimeStatus:
        """Validate and persist a cleared selection without constructing the old runtime."""
        with store.transaction():
            selection = store.selection_without(scope)
            if selection is None:
                store.clear_active(scope=scope)
                return _fake_status()
            loaded = store.get_profile_by_id(selection.profile_id)
            route = resolve_profile_route(loaded, environment=environment)
            candidate = provider_factory(route, environment=environment)
            try:
                store.clear_active(scope=scope)
            except Exception:
                _close_provider(candidate)
                raise
        _close_provider(candidate)
        return _status_for_route(
            route,
            profile=loaded,
            source=selection.source,
            environment=environment,
            model_override=None,
        )

    @property
    def current_provider(self) -> ConversationProvider:
        """Return the current provider for initial loop compatibility wiring."""
        with self._lock:
            self._ensure_open()
            return self._provider

    @property
    def store(self) -> ProviderProfileStore:
        """Return the profile store used by this manager."""
        return self._store

    @contextmanager
    def provider_for_turn(self) -> Iterator[ConversationProvider]:
        """Pin and yield the current client for one complete conversation turn."""
        with self._lock:
            self._ensure_open()
            if self._turn_active:
                raise RuntimeProviderStateError("a conversation turn is already active")
            self._turn_active = True
            provider = self._provider
        try:
            yield provider
        finally:
            with self._lock:
                self._turn_active = False

    def use_profile(self, name: str, *, scope: str = "project") -> RuntimeStatus:
        """Construct, persist, and commit one effective profile switch atomically."""
        with self._lock, self._store.transaction():
            self._ensure_switchable()
            requested = self._store.get_profile(name)
            selection = self._store.selection_with_id(requested.profile_id, scope)
            loaded = self._store.get_profile_by_id(selection.profile_id)
            route = resolve_profile_route(loaded, environment=self._environment)
            candidate = self._construct(route)
            try:
                self._store.set_active_id(requested.profile_id, scope=scope)
            except Exception:
                _close_provider(candidate)
                raise
            old = self._provider
            self._provider = candidate
            self._route = route
            self._load_profile(loaded)
            self._model_override = None
            self._direct_route = None
            self._selection_source = selection.source
            _close_provider(old)
            return self.status()

    def clear_active(self, *, scope: str = "project") -> RuntimeStatus:
        """Activate the next layer and commit its persisted clear only after construction."""
        with self._lock, self._store.transaction():
            self._ensure_switchable()
            selection = self._store.selection_without(scope)
            if selection is None:
                candidate = self._fake_factory()
                route = None
                loaded = None
                source = "default"
            else:
                loaded = self._store.get_profile_by_id(selection.profile_id)
                route = resolve_profile_route(loaded, environment=self._environment)
                candidate = self._construct(route)
                source = selection.source
            try:
                self._store.clear_active(scope=scope)
            except Exception:
                _close_provider(candidate)
                raise
            old = self._provider
            self._provider = candidate
            self._route = route
            if loaded is None:
                self._profile_id = None
                self._loaded_profile = None
            else:
                self._load_profile(loaded)
            self._model_override = None
            self._direct_route = None
            self._selection_source = source
            _close_provider(old)
            return self.status()

    def set_model(self, model: str) -> RuntimeStatus:
        """Apply a non-persistent override, reloading profile configuration by stable ID."""
        with self._lock:
            self._ensure_switchable()
            if self._profile_id is not None:
                loaded = self._store.get_profile_by_id(self._profile_id)
                route = resolve_profile_route(
                    loaded,
                    environment=self._environment,
                    model_override=model,
                )
            elif self._direct_route is not None:
                loaded = None
                route = _route_with_model(self._direct_route, model)
            else:
                raise RuntimeProviderStateError("model override requires a real provider runtime")
            candidate = self._construct(route)
            old = self._provider
            self._provider = candidate
            self._route = route
            if loaded is not None:
                self._load_profile(loaded)
            self._model_override = model
            _close_provider(old)
            return self.status()

    def status(self) -> RuntimeStatus:
        """Return a redacted snapshot with profile and resolved-route provenance."""
        with self._lock:
            route = self._route
            if route is None:
                return _fake_status(source=self._selection_source)
            return _status_for_route(
                route,
                profile=self._loaded_profile,
                source=self._selection_source,
                environment=self._environment,
                model_override=self._model_override,
            )

    def close(self) -> None:
        """Close the active client once; subsequent operations are rejected."""
        with self._lock:
            if self._closed:
                return
            if self._turn_active:
                raise RuntimeProviderStateError(
                    "cannot close provider runtime during a conversation turn"
                )
            self._closed = True
            provider = self._provider
        _close_provider(provider)

    def _construct(self, route: RuntimeProviderRoute) -> ConversationProvider:
        return self._provider_factory(route, environment=self._environment)

    def _load_profile(self, profile: NamedProviderProfile) -> None:
        self._profile_id = profile.profile_id
        self._loaded_profile = profile

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeProviderStateError("provider runtime is closed")

    def _ensure_switchable(self) -> None:
        self._ensure_open()
        if self._turn_active:
            raise RuntimeProviderStateError("cannot switch provider during a conversation turn")


def _route_with_model(route: RuntimeProviderRoute, model: str) -> RuntimeProviderRoute:
    if not model.strip():
        raise RuntimeProviderStateError("model override must not be blank")
    if model != model.strip():
        raise RuntimeProviderStateError("model override must not have surrounding whitespace")
    provider_id = route.definition.provider_id
    prefix = f"{provider_id}/"
    wire_model = model[len(prefix) :] if model.startswith(prefix) else model
    if not wire_model:
        raise RuntimeProviderStateError("model override must include a model ID")
    return replace(route, selected_model=model, wire_model=wire_model)


def _fake_status(*, source: str = "default") -> RuntimeStatus:
    return RuntimeStatus(
        mode="fake",
        profile=None,
        selection_source=source,
        provider_id="fake",
        protocol=None,
        selected_model=None,
        wire_model=None,
        base_url=None,
        base_url_source=None,
        credential_required=False,
        credential_present=False,
    )


def _status_for_route(
    route: RuntimeProviderRoute,
    *,
    profile: NamedProviderProfile | None,
    source: str,
    environment: Mapping[str, str],
    model_override: str | None,
) -> RuntimeStatus:
    definition = route.definition
    present = bool(
        definition.credential_env and environment.get(definition.credential_env, "").strip()
    )
    return RuntimeStatus(
        mode="real",
        profile=profile.name if profile is not None else None,
        selection_source=source,
        provider_id=definition.provider_id,
        protocol=definition.protocol.value,
        selected_model=route.selected_model,
        wire_model=route.wire_model,
        base_url=route.base_url,
        base_url_source=route.base_url_source,
        credential_required=definition.credential_required,
        credential_present=present,
        credential_env=definition.credential_env,
        max_output_tokens=route.max_output_tokens,
        temperature=route.temperature,
        profile_id=profile.profile_id if profile is not None else None,
        profile_revision=profile.revision if profile is not None else None,
        profile_fingerprint=profile.fingerprint() if profile is not None else None,
        route_fingerprint=route.fingerprint(),
        model_override=model_override,
    )


def _close_provider(provider: ConversationProvider) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
