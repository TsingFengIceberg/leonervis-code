"""Long-lived provider client management with atomic between-turn switching."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from leonervis_code.core.contracts import (
    ConversationProvider,
    ConversationRequest,
    ProviderResponse,
)
from leonervis_code.providers.definitions import ADAPTER_CONTRACT_VERSION, RuntimeProviderRoute
from leonervis_code.providers.factory import create_provider
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.providers.model_context import (
    ModelContextCapability,
    ModelContextCapabilityResolver,
)
from leonervis_code.providers.model_context_cache import (
    ModelContextCapabilityCache,
    default_model_context_cache_path,
)
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.providers.request_context import (
    RequestTokenCount,
    evaluate_context_fit,
    raise_for_context_fit,
)
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
    context_window_tokens: int | None = None
    context_window_source: str = "unknown"
    context_window_discovered_at: str | None = None
    context_window_expires_at: str | None = None
    context_window_diagnostic: str | None = None
    model_max_output_tokens: int | None = None
    model_max_output_source: str = "unknown"
    model_max_output_diagnostic: str | None = None
    adapter_contract_version: int = ADAPTER_CONTRACT_VERSION

    @property
    def profile_name(self) -> str | None:
        """Expose an explicit name while retaining the existing ``profile`` field."""
        return self.profile


@dataclass(frozen=True)
class TurnRuntimeSnapshot:
    """One immutable provider target pinned for a complete conversation turn."""

    provider: ConversationProvider
    route: RuntimeProviderRoute | None
    capability: ModelContextCapability
    status: RuntimeStatus

    def respond(self, request: ConversationRequest) -> ProviderResponse:
        if self.route is None:
            return self.provider.respond(request)
        input_count = RequestTokenCount.unknown("input counting was not required")
        preliminary = evaluate_context_fit(
            target=self.capability.target,
            input_count=input_count,
            requested_output_tokens=self.route.max_output_tokens,
            context_window_limit=self.capability.context_window_tokens,
            model_output_limit=self.capability.model_max_output_tokens,
        )
        raise_for_context_fit(preliminary)
        if self.capability.context_window_tokens is not None:
            operation = getattr(self.provider, "count_input_tokens", None)
            input_count = (
                operation(request)
                if callable(operation)
                else RequestTokenCount.unknown("provider does not expose input counting")
            )
        report = evaluate_context_fit(
            target=self.capability.target,
            input_count=input_count,
            requested_output_tokens=self.route.max_output_tokens,
            context_window_limit=self.capability.context_window_tokens,
            model_output_limit=self.capability.model_max_output_tokens,
        )
        raise_for_context_fit(report)
        return self.provider.respond(request)


@dataclass(frozen=True)
class _Candidate:
    route: RuntimeProviderRoute
    provider: ConversationProvider
    capability: ModelContextCapability


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
        context_resolver: ModelContextCapabilityResolver | None = None,
        context_cache_path: Path | None = None,
    ) -> None:
        self._store = store
        self._environment = environment
        self._provider_factory = provider_factory
        self._fake_factory = fake_factory
        self._context_resolver = context_resolver or ModelContextCapabilityResolver(
            ModelContextCapabilityCache(
                context_cache_path or default_model_context_cache_path(dict(environment))
            )
        )
        self._lock = RLock()
        self._turn_active = False
        self._closed = False
        self._generation = 0
        self._profile_id: str | None = None
        self._loaded_profile: NamedProviderProfile | None = None
        self._model_override: str | None = None
        self._direct_route: RuntimeProviderRoute | None = None
        self._selection_source = "default"
        self._route: RuntimeProviderRoute | None = None
        self._capability = ModelContextCapability.unknown(None)

        if profile is not None:
            selected_profile = store.get_profile(profile)
            route = resolve_profile_route(
                selected_profile, environment=environment, model_override=model
            )
            candidate = self._prepare_candidate(
                route,
                *_profile_overrides(selected_profile, model_override=model),
            )
            self._load_profile(selected_profile)
            self._model_override = model
            self._selection_source = "cli"
            self._activate(candidate)
        elif model is not None:
            route = resolve_runtime_route(
                model,
                environment=environment,
                custom_protocol=custom_protocol,
                custom_base_url=custom_base_url,
                custom_api_key_env=custom_api_key_env,
            )
            candidate = self._prepare_candidate(route, None)
            self._direct_route = route
            self._selection_source = "cli"
            self._activate(candidate)
        else:
            active = store.active_selection()
            if active is None:
                self._provider = fake_factory()
            else:
                selected_profile = store.get_profile_by_id(active.profile_id)
                route = resolve_profile_route(selected_profile, environment=environment)
                candidate = self._prepare_candidate(
                    route,
                    selected_profile.context_window_tokens,
                    selected_profile.model_max_output_tokens,
                )
                self._load_profile(selected_profile)
                self._selection_source = active.source
                self._activate(candidate)

    @classmethod
    def prepare_profile(
        cls,
        store: ProviderProfileStore,
        name: str,
        *,
        scope: str,
        environment: Mapping[str, str],
        provider_factory: ProviderFactory = create_provider,
        context_resolver: ModelContextCapabilityResolver | None = None,
        context_cache_path: Path | None = None,
    ) -> RuntimeStatus:
        """Prepare outside locks, then validate and persist one profile selection."""
        requested = store.get_profile(name)
        selection = store.selection_with_id(requested.profile_id, scope)
        loaded = store.get_profile_by_id(selection.profile_id)
        route = resolve_profile_route(loaded, environment=environment)
        resolver = context_resolver or ModelContextCapabilityResolver(
            ModelContextCapabilityCache(
                context_cache_path or default_model_context_cache_path(dict(environment))
            )
        )
        candidate = _prepare_external_candidate(
            route,
            loaded.context_window_tokens,
            loaded.model_max_output_tokens,
            environment,
            provider_factory,
            resolver,
        )
        try:
            with store.transaction():
                current_requested = store.get_profile_by_id(requested.profile_id)
                current_selection = store.selection_with_id(current_requested.profile_id, scope)
                current_loaded = store.get_profile_by_id(current_selection.profile_id)
                if (
                    current_requested.revision != requested.revision
                    or current_loaded.profile_id != loaded.profile_id
                    or current_loaded.revision != loaded.revision
                ):
                    raise RuntimeProviderStateError(
                        "provider profile changed during runtime preparation"
                    )
                store.set_active_id(requested.profile_id, scope=scope)
        except Exception:
            _close_provider(candidate.provider)
            raise
        _close_provider(candidate.provider)
        return _status_for_route(
            route,
            profile=loaded,
            source=selection.source,
            environment=environment,
            model_override=None,
            capability=candidate.capability,
        )

    @classmethod
    def prepare_clear(
        cls,
        store: ProviderProfileStore,
        *,
        scope: str,
        environment: Mapping[str, str],
        provider_factory: ProviderFactory = create_provider,
        context_resolver: ModelContextCapabilityResolver | None = None,
        context_cache_path: Path | None = None,
    ) -> RuntimeStatus:
        """Prepare the next layer outside locks, then persist a validated clear."""
        selection = store.selection_without(scope)
        if selection is None:
            with store.transaction():
                if store.selection_without(scope) is not None:
                    raise RuntimeProviderStateError(
                        "provider selection changed during runtime preparation"
                    )
                store.clear_active(scope=scope)
            return _fake_status()
        loaded = store.get_profile_by_id(selection.profile_id)
        route = resolve_profile_route(loaded, environment=environment)
        resolver = context_resolver or ModelContextCapabilityResolver(
            ModelContextCapabilityCache(
                context_cache_path or default_model_context_cache_path(dict(environment))
            )
        )
        candidate = _prepare_external_candidate(
            route,
            loaded.context_window_tokens,
            loaded.model_max_output_tokens,
            environment,
            provider_factory,
            resolver,
        )
        try:
            with store.transaction():
                current = store.selection_without(scope)
                if (
                    current is None
                    or current.profile_id != selection.profile_id
                    or current.revision != selection.revision
                ):
                    raise RuntimeProviderStateError(
                        "provider selection changed during runtime preparation"
                    )
                store.clear_active(scope=scope)
        except Exception:
            _close_provider(candidate.provider)
            raise
        _close_provider(candidate.provider)
        return _status_for_route(
            route,
            profile=loaded,
            source=selection.source,
            environment=environment,
            model_override=None,
            capability=candidate.capability,
        )

    @property
    def current_provider(self) -> ConversationProvider:
        with self._lock:
            self._ensure_open()
            return self._provider

    @property
    def store(self) -> ProviderProfileStore:
        return self._store

    @contextmanager
    def provider_for_turn(self) -> Iterator[TurnRuntimeSnapshot]:
        """Pin and yield the complete runtime for one conversation turn."""
        with self._lock:
            self._ensure_open()
            if self._turn_active:
                raise RuntimeProviderStateError("a conversation turn is already active")
            self._turn_active = True
            route = self._route
            capability = self._capability
            status = (
                _fake_status(source=self._selection_source)
                if route is None
                else _status_for_route(
                    route,
                    profile=self._loaded_profile,
                    source=self._selection_source,
                    environment=self._environment,
                    model_override=self._model_override,
                    capability=capability,
                )
            )
            snapshot = TurnRuntimeSnapshot(self._provider, route, capability, status)
        try:
            yield snapshot
        finally:
            with self._lock:
                self._turn_active = False

    def use_profile(self, name: str, *, scope: str = "project") -> RuntimeStatus:
        """Prepare outside locks and atomically commit one effective profile switch."""
        with self._lock:
            self._ensure_switchable()
            generation = self._generation
        requested = self._store.get_profile(name)
        selection = self._store.selection_with_id(requested.profile_id, scope)
        loaded = self._store.get_profile_by_id(selection.profile_id)
        route = resolve_profile_route(loaded, environment=self._environment)
        candidate = self._prepare_candidate(
            route, loaded.context_window_tokens, loaded.model_max_output_tokens
        )
        try:
            with self._lock, self._store.transaction():
                self._ensure_switchable()
                if self._generation != generation:
                    raise RuntimeProviderStateError(
                        "provider runtime changed during switch preparation"
                    )
                current_requested = self._store.get_profile_by_id(requested.profile_id)
                current_selection = self._store.selection_with_id(
                    current_requested.profile_id, scope
                )
                current_loaded = self._store.get_profile_by_id(current_selection.profile_id)
                if (
                    current_requested.revision != requested.revision
                    or current_loaded.profile_id != loaded.profile_id
                    or current_loaded.revision != loaded.revision
                ):
                    raise RuntimeProviderStateError(
                        "provider profile changed during switch preparation"
                    )
                self._store.set_active_id(requested.profile_id, scope=scope)
                old = self._provider
                self._activate(candidate)
                self._load_profile(loaded)
                self._model_override = None
                self._direct_route = None
                self._selection_source = selection.source
                self._generation += 1
        except Exception:
            _close_provider(candidate.provider)
            raise
        _close_provider(old)
        return self.status()

    def clear_active(self, *, scope: str = "project") -> RuntimeStatus:
        """Prepare the next layer outside locks and atomically commit a clear."""
        with self._lock:
            self._ensure_switchable()
            generation = self._generation
        selection = self._store.selection_without(scope)
        if selection is None:
            candidate_provider = self._fake_factory()
            candidate = None
            loaded = None
            source = "default"
        else:
            loaded = self._store.get_profile_by_id(selection.profile_id)
            route = resolve_profile_route(loaded, environment=self._environment)
            candidate = self._prepare_candidate(
                route, loaded.context_window_tokens, loaded.model_max_output_tokens
            )
            candidate_provider = candidate.provider
            source = selection.source
        try:
            with self._lock, self._store.transaction():
                self._ensure_switchable()
                if self._generation != generation:
                    raise RuntimeProviderStateError(
                        "provider runtime changed during switch preparation"
                    )
                current = self._store.selection_without(scope)
                if (current is None) != (selection is None) or (
                    current is not None
                    and selection is not None
                    and (current.profile_id, current.revision)
                    != (selection.profile_id, selection.revision)
                ):
                    raise RuntimeProviderStateError(
                        "provider selection changed during switch preparation"
                    )
                self._store.clear_active(scope=scope)
                old = self._provider
                self._provider = candidate_provider
                if candidate is None:
                    self._route = None
                    self._capability = ModelContextCapability.unknown(None)
                else:
                    self._route = candidate.route
                    self._capability = candidate.capability
                if loaded is None:
                    self._profile_id = None
                    self._loaded_profile = None
                else:
                    self._load_profile(loaded)
                self._model_override = None
                self._direct_route = None
                self._selection_source = source
                self._generation += 1
        except Exception:
            _close_provider(candidate_provider)
            raise
        _close_provider(old)
        return self.status()

    def set_model(self, model: str) -> RuntimeStatus:
        """Prepare a process-local model override and commit it atomically."""
        with self._lock:
            self._ensure_switchable()
            generation = self._generation
            profile_id = self._profile_id
            direct_route = self._direct_route
        if profile_id is not None:
            loaded = self._store.get_profile_by_id(profile_id)
            route = resolve_profile_route(
                loaded, environment=self._environment, model_override=model
            )
        elif direct_route is not None:
            loaded = None
            route = _route_with_model(direct_route, model)
        else:
            raise RuntimeProviderStateError("model override requires a real provider runtime")
        candidate = self._prepare_candidate(route, None)
        try:
            with self._lock:
                self._ensure_switchable()
                if self._generation != generation:
                    raise RuntimeProviderStateError(
                        "provider runtime changed during switch preparation"
                    )
                if loaded is not None:
                    current = self._store.get_profile_by_id(loaded.profile_id)
                    if current.revision != loaded.revision:
                        raise RuntimeProviderStateError(
                            "provider profile changed during switch preparation"
                        )
                old = self._provider
                self._activate(candidate)
                if loaded is not None:
                    self._load_profile(loaded)
                self._model_override = model
                self._generation += 1
        except Exception:
            _close_provider(candidate.provider)
            raise
        _close_provider(old)
        return self.status()

    def status(self) -> RuntimeStatus:
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
                capability=self._capability,
            )

    def close(self) -> None:
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

    def _prepare_candidate(
        self,
        route: RuntimeProviderRoute,
        profile_override: int | None,
        model_max_output_override: int | None = None,
    ) -> _Candidate:
        return _prepare_external_candidate(
            route,
            profile_override,
            model_max_output_override,
            self._environment,
            self._provider_factory,
            self._context_resolver,
        )

    def _activate(self, candidate: _Candidate) -> None:
        self._provider = candidate.provider
        self._route = candidate.route
        self._capability = candidate.capability

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


def _prepare_external_candidate(
    route: RuntimeProviderRoute,
    profile_override: int | None,
    model_max_output_override: int | None,
    environment: Mapping[str, str],
    provider_factory: ProviderFactory,
    resolver: ModelContextCapabilityResolver,
) -> _Candidate:
    provider = provider_factory(route, environment=environment)
    try:
        capability = resolver.resolve(
            route,
            profile_override=profile_override,
            model_max_output_override=model_max_output_override,
            discoverer=provider,
        )
    except Exception:
        _close_provider(provider)
        raise
    return _Candidate(route, provider, capability)


def _profile_overrides(
    profile: NamedProviderProfile, *, model_override: str | None
) -> tuple[int | None, int | None]:
    if model_override is not None:
        return None, None
    return profile.context_window_tokens, profile.model_max_output_tokens


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
    capability: ModelContextCapability,
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
        context_window_tokens=capability.context_window_tokens,
        context_window_source=capability.source.value,
        context_window_discovered_at=capability.discovered_at,
        context_window_expires_at=capability.expires_at,
        context_window_diagnostic=capability.diagnostic,
        model_max_output_tokens=capability.model_max_output_tokens,
        model_max_output_source=capability.model_max_output_source.value,
        model_max_output_diagnostic=capability.model_max_output_diagnostic,
    )


def _close_provider(provider: ConversationProvider) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
