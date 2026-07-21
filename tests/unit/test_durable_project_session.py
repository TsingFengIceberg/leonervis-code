from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from leonervis_code.core.contracts import AssistantText, UserMessage
from leonervis_code.providers.definitions import WireProtocol
from leonervis_code.providers.manager import RuntimeSwitchAuditError
from leonervis_code.providers.profile import ProviderProfileSpec
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.session import ProjectSession
from leonervis_code.session_store import SessionStore, SessionStoreError
from leonervis_code.system_prompt import build_system_prompt

SESSION_ONE = "12345678-1234-4234-9234-123456789abc"
SESSION_TWO = "22345678-1234-4234-9234-123456789abc"
NOW = "2026-07-17T12:00:00.000000Z"


@dataclass
class RecordingProvider:
    label: str
    requests: list = None

    def __post_init__(self) -> None:
        self.requests = []

    def respond(self, request):
        self.requests.append(request)
        return AssistantText(f"{self.label}: {request.history[-1].text}")


def session_store_factory(*ids: str):
    values = iter(ids)

    def factory(workspace: Path) -> SessionStore:
        return SessionStore(
            workspace,
            uuid_factory=lambda: UUID(next(values)),
            clock=lambda: NOW,
        )

    return factory


def test_project_session_persists_and_resumes_history_with_current_runtime(tmp_path: Path) -> None:
    first = ProjectSession.open(
        tmp_path,
        environment={},
        user_profile_path=tmp_path / "user.json",
        project_profile_path=tmp_path / "project.json",
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    assert first.prompt("hello") == "Fake response: hello"
    transcript = first.transcript_path
    first.close()

    second = ProjectSession.open(
        tmp_path,
        resume=SESSION_ONE,
        environment={},
        user_profile_path=tmp_path / "user.json",
        project_profile_path=tmp_path / "project.json",
        session_store_factory=session_store_factory(SESSION_TWO),
    )

    assert second.history == (UserMessage("hello"), AssistantText("Fake response: hello"))
    assert second.prompt("again") == "Fake response: again"
    assert second.transcript_path == transcript
    second.close()


def test_project_session_resume_does_not_restore_historical_provider_binding(
    tmp_path: Path,
) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    stored = store.add_profile(
        ProviderProfileSpec(
            name="one",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="model-one",
            base_url="http://127.0.0.1:11434/v1",
        )
    )
    providers = []

    def factory(route, *, environment):
        provider = RecordingProvider(route.wire_model)
        providers.append(provider)
        return provider

    first = ProjectSession.open(
        tmp_path,
        profile="one",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=factory,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    assert first.prompt("first") == "model-one: first"
    first_prompt = providers[0].requests[0].system_prompt
    first.close()

    store.remove_profile_by_id(stored.profile_id)
    resumed = ProjectSession.open(
        tmp_path,
        resume=SESSION_ONE,
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        session_store_factory=session_store_factory(SESSION_TWO),
    )

    assert resumed.status().mode == "fake"
    assert resumed.history[-1] == AssistantText("model-one: first")
    assert first_prompt == build_system_prompt()
    assert all(first_prompt.text not in repr(item) for item in resumed.history)
    assert resumed.prompt("second") == "Fake response: second"
    resumed.close()


def test_project_session_switches_durable_history_without_changing_runtime(tmp_path: Path) -> None:
    provider = RecordingProvider("runtime")
    factory = session_store_factory(SESSION_ONE, SESSION_TWO)
    session = ProjectSession.open(
        tmp_path,
        model="local/model",
        environment={},
        provider_factory=lambda route, *, environment: provider,
        session_store_factory=factory,
    )
    session.prompt("one")
    first_id = session.session_id
    assert session.latest_session_info().session_id == first_id
    second_id = session.new_session().session_id
    assert session.latest_session_info().session_id == second_id
    session.prompt("two")
    assert session.history == (UserMessage("two"), AssistantText("runtime: two"))
    session.switch_session(first_id)
    assert session.latest_session_info().session_id == first_id

    info = session.switch_session(second_id)

    assert info.session_id == second_id
    assert session.latest_session_info().session_id == second_id
    assert session.status().selected_model == "local/model"
    assert session.history == (UserMessage("two"), AssistantText("runtime: two"))
    session.close()


def test_context_inspection_does_not_mutate_session_or_transcript(tmp_path: Path) -> None:
    session = ProjectSession.open(
        tmp_path,
        environment={},
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    session.prompt("hello")
    before_bytes = session.transcript_path.read_bytes()
    before_info = session.session_info()
    before_history = session.history
    before_status = session.status()

    first = session.inspect_context()
    second = session.inspect_context()

    assert first.context_id == second.context_id
    assert first.full_turn_count == first.effective_turn_count == 1
    assert first.full_item_count == first.effective_item_count == 2
    assert first.fit_decision.value == "unknown"
    assert session.history == before_history
    assert session.effective_history == before_history
    assert session.status() == before_status
    assert session.session_info() == before_info
    assert session.transcript_path.read_bytes() == before_bytes
    session.close()


def test_runtime_switch_records_real_generation_and_reports_audit_failure(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="one",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="model-one",
            base_url="http://127.0.0.1:11434/v1",
        )
    )
    store.add_profile(
        ProviderProfileSpec(
            name="two",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="model-two",
            base_url="http://127.0.0.1:11435/v1",
        )
    )
    session = ProjectSession.open(
        tmp_path,
        profile="one",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: RecordingProvider(route.wire_model),
        session_store_factory=session_store_factory(SESSION_ONE),
    )

    result = session.use_profile("two")

    assert result.status.generation == 1
    assert session._writer.state.records[-1].binding.generation == 1

    session._writer.release()
    with pytest.raises(RuntimeSwitchAuditError) as caught:
        session.set_model("model-three")
    assert caught.value.result.status.selected_model == "model-three"
    assert caught.value.result.status.generation == 2
    assert session.status().selected_model == "model-three"
    session._closed = True
    session._manager.close()


def test_project_session_durable_append_failure_does_not_commit_memory(tmp_path: Path) -> None:
    session = ProjectSession.open(
        tmp_path,
        environment={},
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    session._writer.release()

    with pytest.raises(SessionStoreError, match="released"):
        session.prompt("lost")

    assert session.history == ()
    assert session.turns == ()
    session._closed = True
    session._manager.close()
