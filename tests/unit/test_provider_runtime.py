from __future__ import annotations

from dataclasses import dataclass

import pytest

from leonervis_code.core.contracts import AssistantText, ToolResult, ToolUse, UserMessage
from leonervis_code.providers.definitions import WireProtocol
from leonervis_code.providers.manager import RuntimeProviderManager, RuntimeProviderStateError
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.session import ProjectSession


@dataclass
class RecordingProvider:
    label: str
    closed: bool = False

    def __post_init__(self) -> None:
        self.histories = []

    def respond(self, history):
        self.histories.append(history)
        return AssistantText(text=f"{self.label}: {history[-1].text}")

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


def test_manager_reuses_client_and_atomically_switches_profiles(tmp_path) -> None:
    store = configured_store(tmp_path)
    constructed = []

    def factory(route, *, environment):
        provider = RecordingProvider(route.wire_model)
        constructed.append(provider)
        return provider

    manager = RuntimeProviderManager(store, environment={}, profile="one", provider_factory=factory)
    with manager.provider_for_turn() as first:
        assert first is constructed[0]
        with pytest.raises(RuntimeProviderStateError, match="during a conversation turn"):
            manager.use_profile("two")
    status = manager.use_profile("two")

    assert status.profile == "two"
    assert status.selected_model == "model-two"
    assert store.active_name("project") == "two"
    assert constructed[0].closed is True
    with manager.provider_for_turn() as current:
        assert current is constructed[1]


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
    assert providers["model-two"].histories[0][:2] == session.history[:2]


def test_user_scope_switch_respects_existing_project_precedence(tmp_path) -> None:
    store = configured_store(tmp_path)
    store.set_active("one", scope="project")
    providers = []

    def factory(route, *, environment):
        provider = RecordingProvider(route.wire_model)
        providers.append(provider)
        return provider

    manager = RuntimeProviderManager(store, environment={}, provider_factory=factory)
    status = manager.use_profile("two", scope="user")

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
    status = manager.set_model("model-two")

    assert status.profile is None
    assert status.selected_model == "model-two"
    assert constructed[-1].wire_model == "model-two"


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

        def respond(self, history):
            self.calls += 1
            if self.calls == 1:
                return ToolUse("call-1", "read_file", "README.md")
            assert history[-1] == ToolResult("call-1", "notes\n")
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
    assert session.status().credential_present is False
