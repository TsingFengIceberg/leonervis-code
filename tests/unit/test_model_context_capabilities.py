from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace

from leonervis_code.providers.definitions import (
    ANTHROPIC,
    OPENAI,
    RuntimeProviderRoute,
    WireProtocol,
)
from leonervis_code.providers.model_context import (
    ModelContextCapabilityResolver,
    ModelContextDiscovery,
    ModelContextSource,
)
from leonervis_code.providers.model_context_cache import ModelContextCapabilityCache

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def route(
    *,
    definition=ANTHROPIC,
    model="unknown-claude-model",
    base_url="https://api.anthropic.com",
) -> RuntimeProviderRoute:
    return RuntimeProviderRoute(
        definition=definition,
        selected_model=model,
        wire_model=model,
        base_url=base_url,
        base_url_source="test",
    )


class RecordingDiscoverer:
    def __init__(self, value: int | None, diagnostic: str | None = None) -> None:
        self.calls = 0
        self.value = value
        self.diagnostic = diagnostic

    def discover_model_context(self) -> ModelContextDiscovery:
        self.calls += 1
        return ModelContextDiscovery(self.value, self.diagnostic)


def test_resolution_precedence_and_exact_catalog_matching(tmp_path) -> None:
    cache = ModelContextCapabilityCache(tmp_path / "cache.json")
    resolver = ModelContextCapabilityResolver(cache, clock=lambda: NOW)
    known = route(model="claude-opus-4-8")
    discoverer = RecordingDiscoverer(777)

    override = resolver.resolve(known, profile_override=123_456, discoverer=discoverer)
    assert override.context_window_tokens == 123_456
    assert override.source == ModelContextSource.PROFILE_OVERRIDE

    builtin = resolver.resolve(known, discoverer=discoverer)
    assert builtin.context_window_tokens == 1_000_000
    assert builtin.source == ModelContextSource.BUILTIN_CATALOG
    assert discoverer.calls == 0

    custom_endpoint = route(model="claude-opus-4-8", base_url="https://gateway.example")
    custom_discoverer = RecordingDiscoverer(999_999)
    unknown = resolver.resolve(custom_endpoint, discoverer=custom_discoverer)
    assert unknown.context_window_tokens is None
    assert unknown.source == ModelContextSource.UNKNOWN
    assert custom_discoverer.calls == 0

    compatible_same_name = RuntimeProviderRoute(
        definition=OPENAI,
        selected_model="claude-opus-4-8",
        wire_model="claude-opus-4-8",
        base_url="https://api.openai.com/v1",
        base_url_source="test",
    )
    assert resolver.resolve_offline(compatible_same_name).context_window_tokens is None


def test_live_discovery_is_cached_and_expires(tmp_path) -> None:
    current = [NOW]
    cache = ModelContextCapabilityCache(tmp_path / "cache.json", ttl=timedelta(hours=24))
    first_resolver = ModelContextCapabilityResolver(cache, clock=lambda: current[0])
    requested = route()
    discoverer = RecordingDiscoverer(222_000)

    first = first_resolver.resolve(requested, discoverer=discoverer)
    assert first.source == ModelContextSource.LIVE_DISCOVERY
    assert first.context_window_tokens == 222_000
    assert discoverer.calls == 1

    cached = first_resolver.resolve(requested, discoverer=discoverer)
    assert cached.source == ModelContextSource.DISCOVERY_CACHE
    assert cached.context_window_tokens == 222_000
    assert discoverer.calls == 1

    current[0] = NOW + timedelta(hours=24)
    refreshed = first_resolver.resolve(requested, discoverer=discoverer)
    assert refreshed.source == ModelContextSource.LIVE_DISCOVERY
    assert discoverer.calls == 2


def test_unknown_and_failures_are_not_negative_cached(tmp_path) -> None:
    cache = ModelContextCapabilityCache(tmp_path / "cache.json")
    resolver = ModelContextCapabilityResolver(cache, clock=lambda: NOW)
    requested = route()
    unsupported = RecordingDiscoverer(None, "unsupported")

    assert resolver.resolve(requested, discoverer=unsupported).source == ModelContextSource.UNKNOWN
    assert resolver.resolve(requested, discoverer=unsupported).source == ModelContextSource.UNKNOWN
    assert unsupported.calls == 2
    assert not cache.path.exists()

    class Failing:
        def discover_model_context(self):
            raise RuntimeError("secret raw failure")

    failed = resolver.resolve(requested, discoverer=Failing())
    assert failed.source == ModelContextSource.UNKNOWN
    assert failed.diagnostic == "live context discovery failed safely"
    assert "secret" not in failed.diagnostic


def test_cache_is_private_closed_and_rejects_unsafe_paths(tmp_path) -> None:
    path = tmp_path / "private" / "cache.json"
    cache = ModelContextCapabilityCache(path)
    resolver = ModelContextCapabilityResolver(cache, clock=lambda: NOW)
    requested = route()

    resolver.resolve(requested, discoverer=RecordingDiscoverer(300_000))
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    payload = json.loads(path.read_text(encoding="utf-8"))
    encoded = json.dumps(payload)
    assert "credential_env" in encoded
    assert "api_key" not in encoded

    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    recovered = resolver.resolve(requested, discoverer=RecordingDiscoverer(310_000))
    assert recovered.source == ModelContextSource.LIVE_DISCOVERY
    assert "unavailable or unsafe" in recovered.diagnostic

    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    unsafe = resolver.resolve(requested, discoverer=RecordingDiscoverer(320_000))
    assert unsafe.source == ModelContextSource.LIVE_DISCOVERY
    assert unsafe.context_window_tokens == 320_000
    assert "cache" in unsafe.diagnostic
    assert target.read_text(encoding="utf-8") == "{}"


def test_cache_key_includes_credential_reference_without_value(tmp_path) -> None:
    cache = ModelContextCapabilityCache(tmp_path / "cache.json")
    resolver = ModelContextCapabilityResolver(cache, clock=lambda: NOW)
    first_route = route()
    alternate_definition = SimpleNamespace(
        provider_id="anthropic",
        protocol=WireProtocol.ANTHROPIC_MESSAGES,
        credential_env="ALTERNATE_KEY",
    )
    alternate_route = RuntimeProviderRoute(
        definition=alternate_definition,
        selected_model=first_route.selected_model,
        wire_model=first_route.wire_model,
        base_url=first_route.base_url,
        base_url_source="test",
    )

    resolver.resolve(first_route, discoverer=RecordingDiscoverer(100_000))
    discoverer = RecordingDiscoverer(200_000)
    alternate = resolver.resolve(alternate_route, discoverer=discoverer)

    assert alternate.context_window_tokens == 200_000
    assert discoverer.calls == 1
