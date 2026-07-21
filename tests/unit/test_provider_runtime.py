from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from leonervis_code.core.compaction import (
    CompactSummaryRequest,
    CompactionUnavailableError,
    build_compact_prompt,
)
from leonervis_code.core.contracts import (
    AssistantText,
    ConversationRequest,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.providers.definitions import WireProtocol
from leonervis_code.providers.manager import (
    RuntimeProviderManager,
    RuntimeProviderStateError,
    RuntimeSwitchContextError,
)
from leonervis_code.providers.model_context import (
    ModelContextCapability,
    ModelContextCapabilityResolver,
    ModelContextSource,
    ModelContextTarget,
)
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.providers.request_context import (
    ContextFitDecision,
    ContextPreflightError,
    RequestTokenCount,
    RequestTokenCountMethod,
)
from leonervis_code.session import ProjectSession
from leonervis_code.system_prompt import build_system_prompt


@dataclass
class RecordingProvider:
    label: str
    closed: bool = False

    def __post_init__(self) -> None:
        self.requests = []

    def respond(self, request):
        self.requests.append(request)
        return AssistantText(text=f"{self.label}: {request.history[-1].text}")

    def close(self) -> None:
        self.closed = True


def configured_store(tmp_path) -> ProviderProfileStore:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        NamedProviderProfile(
            name="one",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="model-one",
            base_url="http://127.0.0.1:11434/v1",
        )
    )
    store.add_profile(
        NamedProviderProfile(
            name="two",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="model-two",
            base_url="http://127.0.0.1:11435/v1",
        )
    )
    return store


def test_compaction_runtime_lease_is_real_pinned_and_blocks_switches(tmp_path) -> None:
    store = configured_store(tmp_path)
    profile = store.get_profile("one")
    store.replace_profile(
        profile.profile_id,
        replace(
            profile.to_spec(),
            context_window_tokens=1000,
            model_max_output_tokens=100,
            max_output_tokens=20,
        ),
        expected_revision=profile.revision,
    )

    class CompactProvider(RecordingProvider):
        def count_compact_summary_input_tokens(self, request):
            return RequestTokenCount(10, RequestTokenCountMethod.ESTIMATED)

        def summarize_compact(self, request):
            return AssistantText("summary")

    provider = CompactProvider("one")
    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=lambda route, *, environment: provider,
    )
    request = CompactSummaryRequest(build_compact_prompt(), "source", 20)

    with manager.provider_for_compaction() as runtime:
        assert runtime.status.generation == manager.status().generation
        assert runtime.assess_summary_request(request).decision == ContextFitDecision.FITS
        assert runtime.summarize(request) == AssistantText("summary")
        with pytest.raises(RuntimeProviderStateError, match="active operation"):
            manager.use_profile("two")
    assert manager.use_profile("two").status.profile == "two"


def test_fake_runtime_rejects_controlled_compaction(tmp_path) -> None:
    manager = RuntimeProviderManager(configured_store(tmp_path), environment={})

    with pytest.raises(CompactionUnavailableError, match="real provider"):
        with manager.provider_for_compaction():
            raise AssertionError


def test_manager_reuses_client_and_atomically_switches_profiles(tmp_path) -> None:
    store = configured_store(tmp_path)
    constructed = []

    def factory(route, *, environment):
        provider = RecordingProvider(route.wire_model)
        constructed.append(provider)
        return provider

    manager = RuntimeProviderManager(store, environment={}, profile="one", provider_factory=factory)
    with manager.provider_for_turn() as first:
        assert first.provider is constructed[0]
        with pytest.raises(RuntimeProviderStateError, match="active operation"):
            manager.use_profile("two")
    result = manager.use_profile("two")
    status = result.status

    assert result.fit_report is None
    assert status.profile == "two"
    assert status.profile_name == "two"
    assert status.profile_id == store.get_profile("two").profile_id
    assert status.profile_revision == 1
    assert status.profile_fingerprint == store.get_profile("two").fingerprint()
    assert status.route_fingerprint is not None
    assert len(status.route_fingerprint) == 64
    assert status.model_override is None
    assert status.selected_model == "model-two"
    assert store.active_name("project") == "two"
    assert constructed[0].closed is True
    with manager.provider_for_turn() as current:
        assert current.provider is constructed[1]


def test_manager_failed_switch_preserves_client_and_persistence(tmp_path) -> None:
    store = configured_store(tmp_path)
    first = RecordingProvider("one")

    def factory(route, *, environment):
        if route.wire_model == "model-two":
            raise RuntimeError("construction failed")
        return first

    manager = RuntimeProviderManager(store, environment={}, profile="one", provider_factory=factory)
    with pytest.raises(RuntimeError, match="construction failed"):
        manager.use_profile("two")

    assert manager.status().profile == "one"
    assert store.active_name("project") is None
    assert first.closed is False


def test_project_session_preserves_neutral_history_across_provider_switch(tmp_path) -> None:
    store = configured_store(tmp_path)
    providers = {}

    def factory(route, *, environment):
        provider = RecordingProvider(route.wire_model)
        providers[route.wire_model] = provider
        return provider

    session = ProjectSession.open(
        tmp_path,
        profile="one",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=factory,
    )
    assert session.prompt("first") == "model-one: first"
    session.use_profile("two")
    assert session.prompt("second") == "model-two: second"

    assert session.history == (
        UserMessage("first"),
        AssistantText("model-one: first"),
        UserMessage("second"),
        AssistantText("model-two: second"),
    )
    assert providers["model-two"].requests[0].history[:2] == session.history[:2]
    assert (
        providers["model-one"].requests[0].system_prompt
        == providers["model-two"].requests[0].system_prompt
    )
    assert all(
        request.system_prompt.text not in repr(item)
        for provider in providers.values()
        for request in provider.requests
        for item in request.history
    )


def test_user_scope_switch_respects_existing_project_precedence(tmp_path) -> None:
    store = configured_store(tmp_path)
    store.set_active("one", scope="project")
    providers = []

    def factory(route, *, environment):
        provider = RecordingProvider(route.wire_model)
        providers.append(provider)
        return provider

    manager = RuntimeProviderManager(store, environment={}, provider_factory=factory)
    result = manager.use_profile("two", scope="user")
    status = result.status

    assert store.active_name("user") == "two"
    assert store.active_selection().name == "one"
    assert status.profile == "one"
    assert status.selection_source == "project"
    assert status.selected_model == "model-one"


def test_direct_runtime_supports_process_local_model_switch(tmp_path) -> None:
    store = configured_store(tmp_path)
    constructed = []

    def factory(route, *, environment):
        constructed.append(route)
        return RecordingProvider(route.wire_model)

    manager = RuntimeProviderManager(
        store,
        environment={},
        model="local/model-one",
        provider_factory=factory,
    )
    result = manager.set_model("model-two")
    status = result.status

    assert status.profile is None
    assert status.profile_id is None
    assert status.profile_revision is None
    assert status.profile_fingerprint is None
    assert status.route_fingerprint is not None
    assert status.model_override == "model-two"
    assert status.selected_model == "model-two"
    assert constructed[-1].wire_model == "model-two"


def test_manager_set_model_tracks_profile_by_id_across_rename(tmp_path) -> None:
    store = configured_store(tmp_path)
    original = store.get_profile("one")
    routes = []

    def factory(route, *, environment):
        routes.append(route)
        return RecordingProvider(route.wire_model)

    manager = RuntimeProviderManager(store, environment={}, profile="one", provider_factory=factory)
    renamed = store.rename_profile(original.profile_id, "renamed", expected_revision=1)

    result = manager.set_model("override-model")
    status = result.status

    assert status.profile == "renamed"
    assert status.profile_id == original.profile_id
    assert status.profile_revision == renamed.revision
    assert status.model_override == "override-model"
    assert routes[-1].wire_model == "override-model"


def test_runtime_resolves_profile_override_and_model_override_independently(tmp_path) -> None:
    store = configured_store(tmp_path)
    original = store.get_profile("one")
    store.replace_profile(
        original.profile_id,
        replace(original.to_spec(), context_window_tokens=65_536),
        expected_revision=original.revision,
    )

    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=lambda route, *, environment: RecordingProvider(route.wire_model),
    )
    assert manager.status().context_window_tokens == 65_536
    assert manager.status().context_window_source == ModelContextSource.PROFILE_OVERRIDE

    switched = manager.set_model("other").status
    assert switched.context_window_tokens is None
    assert switched.context_window_source == ModelContextSource.UNKNOWN


def test_runtime_discovery_failure_is_nonfatal_and_redacted(tmp_path) -> None:
    store = configured_store(tmp_path)

    class DiscoveringProvider(RecordingProvider):
        def discover_model_context(self):
            raise RuntimeError("secret provider response")

    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=lambda route, *, environment: DiscoveringProvider(route.wire_model),
        context_resolver=ModelContextCapabilityResolver(),
    )

    status = manager.status()
    assert status.context_window_tokens is None
    assert status.context_window_source == ModelContextSource.UNKNOWN
    assert "secret" not in (status.context_window_diagnostic or "")


def test_preflight_rejects_known_overflow_before_provider_send(tmp_path) -> None:
    store = configured_store(tmp_path)
    original = store.get_profile("one")
    store.replace_profile(
        original.profile_id,
        replace(
            original.to_spec(),
            context_window_tokens=100,
            model_max_output_tokens=80,
            max_output_tokens=20,
        ),
        expected_revision=original.revision,
    )

    class CountingProvider(RecordingProvider):
        def count_input_tokens(self, request):
            return RequestTokenCount(81, RequestTokenCountMethod.ESTIMATED)

    provider = CountingProvider("model-one")
    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=lambda route, *, environment: provider,
    )
    with manager.provider_for_turn() as runtime:
        request = ConversationRequest(build_system_prompt(), (UserMessage("too large"),))
        with pytest.raises(ContextPreflightError, match="input=81"):
            runtime.respond(request)
    assert provider.requests == []


def test_switch_rejects_known_committed_context_overflow_without_changing_state(
    tmp_path,
) -> None:
    store = configured_store(tmp_path)
    target = store.get_profile("two")
    store.replace_profile(
        target.profile_id,
        replace(
            target.to_spec(),
            context_window_tokens=100,
            model_max_output_tokens=80,
            max_output_tokens=20,
        ),
        expected_revision=target.revision,
    )
    providers = []

    class CountingProvider(RecordingProvider):
        def count_input_tokens(self, request):
            return RequestTokenCount(81, RequestTokenCountMethod.ESTIMATED)

    def factory(route, *, environment):
        provider = CountingProvider(route.wire_model)
        providers.append(provider)
        return provider

    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=factory,
    )
    before = manager.status()
    request = ConversationRequest(
        build_system_prompt(),
        (UserMessage("hello"), AssistantText("reply")),
    )

    with pytest.raises(RuntimeSwitchContextError) as caught:
        manager.use_profile("two", committed_context=request)

    assert caught.value.report.decision == ContextFitDecision.CONTEXT_EXCEEDED
    assert manager.status() == before
    assert store.active_name("project") is None
    assert providers[0].closed is False
    assert providers[1].closed is True


def test_switch_allows_unknown_count_with_explicit_report(tmp_path) -> None:
    store = configured_store(tmp_path)
    target = store.get_profile("two")
    store.replace_profile(
        target.profile_id,
        replace(target.to_spec(), context_window_tokens=100),
        expected_revision=target.revision,
    )

    class FailingCounter(RecordingProvider):
        def count_input_tokens(self, request):
            raise RuntimeError("raw provider secret")

    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=lambda route, *, environment: FailingCounter(route.wire_model),
    )
    result = manager.use_profile(
        "two",
        committed_context=ConversationRequest(
            build_system_prompt(),
            (UserMessage("hello"), AssistantText("reply")),
        ),
    )

    assert result.status.profile == "two"
    assert result.fit_report is not None
    assert result.fit_report.decision == ContextFitDecision.UNKNOWN
    assert "secret" not in (result.fit_report.input_count.diagnostic or "")


def test_switch_model_output_limit_precedes_counting(tmp_path) -> None:
    store = configured_store(tmp_path)
    target = store.get_profile("two")
    store.replace_profile(
        target.profile_id,
        replace(
            target.to_spec(),
            context_window_tokens=100,
            max_output_tokens=20,
        ),
        expected_revision=target.revision,
    )
    count_calls = []

    class OutputLimitedResolver:
        def resolve(self, route, **kwargs):
            return ModelContextCapability(
                target=ModelContextTarget.from_route(route),
                context_window_tokens=100,
                source=ModelContextSource.PROFILE_OVERRIDE,
                model_max_output_tokens=10,
                model_max_output_source=ModelContextSource.LIVE_DISCOVERY,
            )

    class CountingProvider(RecordingProvider):
        def count_input_tokens(self, request):
            count_calls.append(request)
            return RequestTokenCount(1, RequestTokenCountMethod.EXACT)

    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=lambda route, *, environment: CountingProvider(route.wire_model),
        context_resolver=OutputLimitedResolver(),
    )

    with pytest.raises(RuntimeSwitchContextError) as caught:
        manager.use_profile(
            "two",
            committed_context=ConversationRequest(build_system_prompt(), ()),
        )

    assert caught.value.report.decision == ContextFitDecision.MODEL_OUTPUT_EXCEEDED
    assert count_calls == []


def test_current_context_assessment_is_read_only_and_never_generates(tmp_path) -> None:
    store = configured_store(tmp_path)
    original = store.get_profile("one")
    store.replace_profile(
        original.profile_id,
        replace(
            original.to_spec(),
            context_window_tokens=100,
            model_max_output_tokens=80,
            max_output_tokens=20,
        ),
        expected_revision=original.revision,
    )

    class CountingProvider(RecordingProvider):
        def count_input_tokens(self, request):
            return RequestTokenCount(70, RequestTokenCountMethod.EXACT)

    provider = CountingProvider("model-one")
    manager = RuntimeProviderManager(
        store,
        environment={},
        profile="one",
        provider_factory=lambda route, *, environment: provider,
    )
    before = manager.status()

    assessment = manager.assess_current_context(
        ConversationRequest(
            build_system_prompt(),
            (UserMessage("hello"), AssistantText("reply")),
        )
    )

    assert assessment.status == before
    assert assessment.fit_report is not None
    assert assessment.fit_report.decision == ContextFitDecision.FITS
    assert assessment.fit_report.input_count.input_tokens == 70
    assert provider.requests == []
    assert manager.status() == before
    with manager.provider_for_turn():
        pass


def test_fake_current_context_assessment_is_explicitly_unavailable(tmp_path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    manager = RuntimeProviderManager(store, environment={})

    assessment = manager.assess_current_context(ConversationRequest(build_system_prompt(), ()))

    assert assessment.status.mode == "fake"
    assert assessment.fit_report is None
    assert "unavailable" in assessment.unavailable_diagnostic


def test_fake_runtime_has_explicit_empty_provenance(tmp_path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    manager = RuntimeProviderManager(store, environment={})

    status = manager.status()

    assert status.mode == "fake"
    assert status.profile_id is None
    assert status.profile_revision is None
    assert status.profile_fingerprint is None
    assert status.route_fingerprint is None
    assert status.model_override is None


def test_session_closes_provider_when_tool_construction_fails(tmp_path) -> None:
    store = configured_store(tmp_path)
    provider = RecordingProvider("model-one")

    with pytest.raises(RuntimeError, match="tool failed"):
        ProjectSession.open(
            tmp_path,
            profile="one",
            environment={},
            user_profile_path=store.user_path,
            project_profile_path=store.project_path,
            provider_factory=lambda route, *, environment: provider,
            read_file_factory=lambda path: (_ for _ in ()).throw(RuntimeError("tool failed")),
        )

    assert provider.closed is True


def test_project_session_pins_provider_for_tool_continuation(tmp_path) -> None:
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    store = configured_store(tmp_path)

    class ToolProvider:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def respond(self, request):
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                return ToolUse("call-1", "read_file", "README.md")
            assert request.history[-1] == ToolResult("call-1", "notes\n")
            return AssistantText("done")

    provider = ToolProvider()
    session = ProjectSession.open(
        tmp_path,
        profile="one",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: provider,
    )

    assert session.prompt("read it") == "done"
    assert provider.calls == 2
    assert provider.requests[0].system_prompt is provider.requests[1].system_prompt
    assert session.status().credential_present is False
