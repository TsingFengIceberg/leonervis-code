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
from leonervis_code.providers.request_context import (
    ContextFitDecision,
    RequestTokenCount,
    RequestTokenCountMethod,
)
from leonervis_code.session import (
    AutoCompactionCommitted,
    AutoCompactionNotApplied,
    AutoCompactionStarted,
    ProjectSession,
    ResumeEffect,
    SessionResumeContextError,
)
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

    def count_input_tokens(self, request):
        value = 100 if request.effective_summary is not None else 1000 + len(request.history)
        return RequestTokenCount(value, RequestTokenCountMethod.ESTIMATED)

    def count_compact_summary_input_tokens(self, request):
        return RequestTokenCount(len(request.source_text), RequestTokenCountMethod.ESTIMATED)

    def summarize_compact(self, request):
        return AssistantText("Earlier turns summarized compactly.")

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


def test_target_aware_resume_rejects_known_overflow_without_mutation(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="tiny",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="tiny-model",
            base_url="http://127.0.0.1:11434/v1",
            context_window_tokens=100,
            model_max_output_tokens=4096,
        )
    )
    first = ProjectSession.open(
        tmp_path,
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    first.prompt("hello")
    target = first.transcript_path
    first.close()
    target_before = target.read_bytes()
    latest = target.parent / "latest.json"
    latest_before = latest.read_bytes()

    with pytest.raises(SessionResumeContextError):
        ProjectSession.open(
            tmp_path,
            resume=SESSION_ONE,
            profile="tiny",
            environment={},
            user_profile_path=store.user_path,
            project_profile_path=store.project_path,
            provider_factory=lambda route, *, environment: RecordingProvider("tiny"),
            session_store_factory=session_store_factory(SESSION_TWO),
        )

    assert target.read_bytes() == target_before
    assert latest.read_bytes() == latest_before


def test_same_current_resume_is_a_mutation_free_noop(tmp_path: Path) -> None:
    session = ProjectSession.open(
        tmp_path,
        environment={},
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    before = session.transcript_path.read_bytes()
    before_records = session.session_info().record_count

    result = session.switch_session(session.session_id)

    assert result.effect == ResumeEffect.ALREADY_CURRENT
    assert session.transcript_path.read_bytes() == before
    assert session.session_info().record_count == before_records
    session.close()


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


def test_manual_compaction_preserves_full_history_and_resumes_effective_checkpoint(
    tmp_path: Path,
) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="compact",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="compact-model",
            base_url="http://127.0.0.1:11434/v1",
            context_window_tokens=100_000,
            model_max_output_tokens=4096,
        )
    )
    provider = RecordingProvider("runtime")
    session = ProjectSession.open(
        tmp_path,
        profile="compact",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: provider,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    for index in range(4):
        session.prompt(f"turn-{index}")
    before_history = session.history
    before_turns = session.turns
    before_bytes = session.transcript_path.read_bytes()

    result = session.compact_context()

    assert result.summarized_turn_count == 2
    assert result.retained_turn_count == 2
    assert result.after_input_tokens < result.before_input_tokens
    assert session.history == before_history
    assert session.turns == before_turns
    assert session.effective_history == before_history[-4:]
    assert session.inspect_context().summary_present
    assert session.inspect_context().context_id.startswith("ctx-v2-")
    assert session.transcript_path.read_bytes().startswith(before_bytes)
    assert session._writer.state.records[-1].record_type == "context_compacted"
    transcript = session.transcript_path
    session.close()

    resumed_provider = RecordingProvider("resumed")
    resumed = ProjectSession.open(
        tmp_path,
        resume=SESSION_ONE,
        profile="compact",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: resumed_provider,
        session_store_factory=session_store_factory(SESSION_TWO),
    )
    assert resumed.transcript_path == transcript
    assert resumed.history == before_history
    assert resumed.effective_history == before_history[-4:]
    assert resumed.inspect_context().summary_present
    resumed.prompt("continue")
    assert resumed_provider.requests[-1].effective_summary is not None
    resumed.close()


def test_resume_screening_counts_compacted_effective_projection_only(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="compact",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="compact-model",
            base_url="http://127.0.0.1:11434/v1",
            context_window_tokens=100_000,
            model_max_output_tokens=4096,
        )
    )
    compact_provider = RecordingProvider("compact")
    session = ProjectSession.open(
        tmp_path,
        profile="compact",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: compact_provider,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    for index in range(4):
        session.prompt(f"turn-{index}")
    session.compact_context()
    session.close()

    class ProjectionProvider(RecordingProvider):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.counted = []

        def count_input_tokens(self, request):
            self.counted.append(request)
            return RequestTokenCount(100, RequestTokenCountMethod.ESTIMATED)

    resumed_provider = ProjectionProvider("resumed")
    resumed = ProjectSession.open(
        tmp_path,
        resume=SESSION_ONE,
        profile="compact",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: resumed_provider,
        session_store_factory=session_store_factory(SESSION_TWO),
    )

    screened = resumed_provider.counted[0]
    assert screened.effective_summary is not None
    assert len(screened.history) == 4
    assert resumed.startup_resume_result.fit_report.decision == ContextFitDecision.FITS
    assert resumed.history != screened.history
    assert resumed_provider.requests == []
    resumed.close()


def test_unknown_target_compatibility_applies_resume_without_generation(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="unknown",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="unknown-model",
            base_url="http://127.0.0.1:11434/v1",
        )
    )
    first = ProjectSession.open(
        tmp_path,
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    first.prompt("hello")
    first.close()
    provider = RecordingProvider("unknown")

    resumed = ProjectSession.open(
        tmp_path,
        resume=SESSION_ONE,
        profile="unknown",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: provider,
        session_store_factory=session_store_factory(SESSION_TWO),
    )

    result = resumed.startup_resume_result
    assert result.fit_report.decision == ContextFitDecision.UNKNOWN
    assert provider.requests == []
    assert resumed.history[-1] == AssistantText("Fake response: hello")
    resumed.close()


def test_pre_turn_high_water_auto_compacts_once_and_preserves_pending_prompt(
    tmp_path: Path,
) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="auto",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="auto-model",
            base_url="http://127.0.0.1:11434/v1",
            max_output_tokens=20,
            context_window_tokens=100,
            model_max_output_tokens=100,
        )
    )

    class AutoProvider(RecordingProvider):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.summary_requests = []

        def count_input_tokens(self, request):
            if request.effective_summary is not None:
                return RequestTokenCount(30, RequestTokenCountMethod.ESTIMATED)
            if request.history and request.history[-1] == UserMessage("trigger"):
                return RequestTokenCount(60, RequestTokenCountMethod.ESTIMATED)
            return RequestTokenCount(10, RequestTokenCountMethod.ESTIMATED)

        def count_compact_summary_input_tokens(self, request):
            return RequestTokenCount(10, RequestTokenCountMethod.ESTIMATED)

        def summarize_compact(self, request):
            self.summary_requests.append(request)
            assert "trigger" not in request.source_text
            return AssistantText("summary")

    provider = AutoProvider("runtime")
    session = ProjectSession.open(
        tmp_path,
        profile="auto",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: provider,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    for index in range(4):
        session.prompt(f"turn-{index}")
    events = []

    response = session.prompt("trigger", event_sink=events.append)

    assert response == "runtime: trigger"
    assert len(provider.summary_requests) == 1
    assert [type(event) for event in events] == [
        AutoCompactionStarted,
        AutoCompactionCommitted,
    ]
    assert events[-1].result.trigger.value == "high_water"
    assert session._writer.state.records[-2].record_type == "context_compacted"
    assert session._writer.state.records[-2].trigger.value == "high_water"
    assert session._writer.state.records[-2].high_water_percent == 80
    assert session._writer.state.records[-1].record_type == "turn_committed"
    assert provider.requests[-1].history[-1] == UserMessage("trigger")
    assert provider.requests[-1].history.count(UserMessage("trigger")) == 1
    assert session.history[-2:] == (
        UserMessage("trigger"),
        AssistantText("runtime: trigger"),
    )
    session.close()


def test_known_overflow_auto_compacts_before_sending_prompt(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="auto",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="auto-model",
            base_url="http://127.0.0.1:11434/v1",
            max_output_tokens=20,
            context_window_tokens=100,
            model_max_output_tokens=100,
        )
    )

    class OverflowProvider(RecordingProvider):
        def count_input_tokens(self, request):
            if request.effective_summary is not None:
                return RequestTokenCount(30, RequestTokenCountMethod.ESTIMATED)
            if request.history and request.history[-1] == UserMessage("overflow"):
                return RequestTokenCount(90, RequestTokenCountMethod.ESTIMATED)
            return RequestTokenCount(10, RequestTokenCountMethod.ESTIMATED)

        def count_compact_summary_input_tokens(self, request):
            return RequestTokenCount(10, RequestTokenCountMethod.ESTIMATED)

    provider = OverflowProvider("runtime")
    session = ProjectSession.open(
        tmp_path,
        profile="auto",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: provider,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    for index in range(4):
        session.prompt(f"turn-{index}")
    events = []

    assert session.prompt("overflow", event_sink=events.append) == "runtime: overflow"

    assert events[0].trigger.value == "overflow"
    assert events[1].result.trigger.value == "overflow"
    assert session._writer.state.records[-2].trigger.value == "overflow"
    assert provider.requests[-1].history[-1] == UserMessage("overflow")
    session.close()


def test_known_overflow_without_compaction_eligibility_sends_no_generation(
    tmp_path: Path,
) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="auto",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="auto-model",
            base_url="http://127.0.0.1:11434/v1",
            max_output_tokens=20,
            context_window_tokens=100,
            model_max_output_tokens=100,
        )
    )

    class OverflowProvider(RecordingProvider):
        def count_input_tokens(self, request):
            return RequestTokenCount(90, RequestTokenCountMethod.ESTIMATED)

    provider = OverflowProvider("runtime")
    session = ProjectSession.open(
        tmp_path,
        profile="auto",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: provider,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    events = []

    with pytest.raises(Exception, match="context preflight rejected"):
        session.prompt("overflow", event_sink=events.append)

    assert provider.requests == []
    assert isinstance(events[-1], AutoCompactionNotApplied)
    assert events[-1].prompt_continues is False
    assert session.history == ()
    session.close()


def test_proactive_auto_compact_failure_continues_known_fitting_turn(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(
        ProviderProfileSpec(
            name="auto",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="auto-model",
            base_url="http://127.0.0.1:11434/v1",
            max_output_tokens=20,
            context_window_tokens=100,
            model_max_output_tokens=100,
        )
    )

    class NonReducingProvider(RecordingProvider):
        def count_input_tokens(self, request):
            return RequestTokenCount(60, RequestTokenCountMethod.ESTIMATED)

        def count_compact_summary_input_tokens(self, request):
            return RequestTokenCount(10, RequestTokenCountMethod.ESTIMATED)

        def summarize_compact(self, request):
            return AssistantText("summary")

    provider = NonReducingProvider("runtime")
    session = ProjectSession.open(
        tmp_path,
        profile="auto",
        environment={},
        user_profile_path=store.user_path,
        project_profile_path=store.project_path,
        provider_factory=lambda route, *, environment: provider,
        session_store_factory=session_store_factory(SESSION_ONE),
    )
    for index in range(4):
        session.prompt(f"turn-{index}")
    events = []

    assert session.prompt("continue", event_sink=events.append) == "runtime: continue"

    assert isinstance(events[0], AutoCompactionStarted)
    assert isinstance(events[1], AutoCompactionNotApplied)
    assert events[1].prompt_continues is True
    assert all(
        record.record_type != "context_compacted" for record in session._writer.state.records
    )
    session.close()


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
