"""Project-facing durable conversation facade for one workspace runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
from threading import RLock

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.core.compaction import (
    COMPACT_MAX_OUTPUT_TOKENS,
    COMPACT_MIN_EFFECTIVE_TURNS,
    COMPACT_RETAINED_TURNS,
    CompactSummaryRequest,
    CompactionCandidateError,
    CompactionConflictError,
    CompactionNotEligibleError,
    EffectiveContextSummary,
    build_compact_prompt,
    build_compact_source_text,
)
from leonervis_code.core.contracts import (
    CommittedTurn,
    ConversationItem,
    ConversationProvider,
    ConversationTurn,
)
from leonervis_code.core.effective_context import (
    COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT,
    EffectiveContextSnapshot,
)
from leonervis_code.providers.manager import (
    CurrentTargetContextAssessment,
    RuntimeProviderManager,
    RuntimeStatus,
    RuntimeSwitchAuditError,
    RuntimeSwitchResult,
)
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.providers.request_context import (
    ContextFitDecision,
    ContextFitReport,
    ContextPreflightError,
    rejects_context_transition,
)
from leonervis_code.session_records import BindingSnapshot, ContextCompacted
from leonervis_code.session_store import (
    LatestUpdateStatus,
    SessionInfo,
    SessionResumeStaleError,
    SessionStore,
    SessionStoreError,
    SessionWriter,
)
from leonervis_code.tools.read_file import ReadFileTool


class ResumeEffect(StrEnum):
    ALREADY_CURRENT = "already_current"
    APPLIED = "applied"
    APPLIED_LATEST_FAILED = "applied_latest_failed"
    APPLIED_LATEST_DURABILITY_UNKNOWN = "applied_latest_durability_unknown"


@dataclass(frozen=True)
class SessionResumeResult:
    info: SessionInfo
    effect: ResumeEffect
    target_assessment: CurrentTargetContextAssessment | None
    context_id: str
    recovery_applied: bool
    latest_status: LatestUpdateStatus
    diagnostic: str | None = None

    @property
    def session_id(self) -> str:
        return self.info.session_id

    @property
    def fit_report(self) -> ContextFitReport | None:
        assessment = self.target_assessment
        return assessment.fit_report if assessment is not None else None


class SessionResumeContextError(RuntimeError):
    """Raised when a destination Session is known not to fit the current target."""

    def __init__(self, info: SessionInfo, context_id: str, report: ContextFitReport) -> None:
        self.info = info
        self.context_id = context_id
        self.report = report
        super().__init__("destination Session is incompatible with the current runtime")


class SessionResumeConflictError(RuntimeError):
    """Raised when a prepared target or current source becomes stale."""


@dataclass(frozen=True)
class CompactContextResult:
    """One committed compaction and its comparable current-target evidence."""

    session_id: str
    checkpoint_sequence: int
    source_context_id: str
    result_context_id: str
    summarized_turn_count: int
    retained_turn_count: int
    full_turn_count: int
    before_input_tokens: int
    after_input_tokens: int
    input_method: str
    fit_decision: ContextFitDecision


@dataclass(frozen=True)
class EffectiveContextInspection:
    """One frozen provider-neutral context and coherent current-target assessment."""

    snapshot: EffectiveContextSnapshot
    target_assessment: CurrentTargetContextAssessment
    checkpoint: ContextCompacted | None = None

    @property
    def source(self) -> str:
        return self.snapshot.source

    @property
    def context_id(self) -> str:
        return self.snapshot.context_id

    @property
    def full_turn_count(self) -> int:
        return self.snapshot.full_turn_count

    @property
    def full_item_count(self) -> int:
        return self.snapshot.full_item_count

    @property
    def effective_turn_count(self) -> int:
        return self.snapshot.effective_turn_count

    @property
    def effective_item_count(self) -> int:
        return self.snapshot.effective_item_count

    @property
    def summary_present(self) -> bool:
        return self.snapshot.effective_summary is not None

    @property
    def retained_turn_count(self) -> int:
        return self.snapshot.effective_turn_count

    @property
    def latest_checkpoint_sequence(self) -> int | None:
        return self.checkpoint.sequence if self.checkpoint is not None else None

    @property
    def fit_report(self):
        return self.target_assessment.fit_report

    @property
    def fit_decision(self) -> ContextFitDecision:
        report = self.fit_report
        return report.decision if report is not None else ContextFitDecision.UNKNOWN

    @property
    def remaining_capacity(self) -> int | None:
        report = self.fit_report
        if (
            report is None
            or report.input_count.input_tokens is None
            or report.context_window_limit is None
        ):
            return None
        return (
            report.context_window_limit
            - report.input_count.input_tokens
            - report.requested_output_tokens
        )


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
        *,
        loop: AgentLoop | None = None,
        startup_resume_result: SessionResumeResult | None = None,
    ) -> None:
        self.workspace = workspace
        self._store = store
        self._manager = manager
        self._session_store = session_store
        self._writer = writer
        self._read_file = read_file
        self._lock = RLock()
        self._closed = False
        self._compaction_active = False
        self._loop = loop or self._new_loop(writer)
        self._startup_resume_result = startup_resume_result

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
            if resume is None:
                writer = session_store.create(binding)
                return cls(resolved_workspace, store, manager, session_store, writer, read_file)
            prepared = session_store.prepare_resume(resume)
            writer_holder: dict[str, SessionWriter] = {}
            try:
                loop = cls._loop_from_state(
                    prepared.state,
                    read_file,
                    commit_turn=lambda turn: writer_holder["writer"].append_turn(
                        turn.items,
                        binding=binding_from_status(manager.status()),
                    ),
                )
                snapshot = loop.effective_context_snapshot()
                with manager.provider_for_context_transition() as runtime:
                    assessment = runtime.assess_context(snapshot.to_conversation_request())
                    report = assessment.fit_report
                    if report is not None and rejects_context_transition(report.decision):
                        raise SessionResumeContextError(prepared.info, snapshot.context_id, report)
                    committed = prepared.commit()
                writer = committed.writer
                writer_holder["writer"] = writer
                result = _resume_result(
                    writer.info,
                    snapshot.context_id,
                    assessment,
                    committed.recovery_applied,
                    committed.latest_status,
                    committed.latest_diagnostic,
                )
                return cls(
                    resolved_workspace,
                    store,
                    manager,
                    session_store,
                    writer,
                    read_file,
                    loop=loop,
                    startup_resume_result=result,
                )
            except BaseException:
                prepared.abort()
                raise
        except BaseException:
            if writer is not None:
                writer.release()
            manager.close()
            raise

    @property
    def startup_resume_result(self) -> SessionResumeResult | None:
        return self._startup_resume_result

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._writer.session_id

    @property
    def transcript_path(self) -> Path:
        with self._lock:
            return self._writer.path

    @property
    def history(self) -> tuple[ConversationItem, ...]:
        with self._lock:
            return self._loop.history

    @property
    def effective_history(self) -> tuple[ConversationItem, ...]:
        with self._lock:
            return self._loop.effective_history

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        with self._lock:
            return self._loop.turns

    def session_info(self) -> SessionInfo:
        self._ensure_open()
        return self._writer.info

    def list_sessions(self) -> tuple[SessionInfo, ...]:
        self._ensure_open()
        return self._session_store.list()

    def latest_session_info(self) -> SessionInfo:
        """Return the Session referenced by this workspace's latest pointer."""
        self._ensure_open()
        return self._session_store.show("latest")

    def new_session(self) -> SessionInfo:
        """Create and atomically select an empty Session without changing runtime."""
        with self._lock:
            self._ensure_open()
            self._ensure_not_compacting()
            candidate = self._session_store.create(binding_from_status(self._manager.status()))
            loop = self._new_loop(candidate)
            old = self._writer
            self._writer = candidate
            self._loop = loop
            old.release()
            return candidate.info

    def switch_session(self, selector: str | Path) -> SessionResumeResult:
        """Screen and atomically swap durable history without changing runtime."""
        with self._lock:
            self._ensure_open()
            self._ensure_not_compacting()
            if _selector_matches_current(selector, self._writer, self._session_store):
                snapshot = self._loop.effective_context_snapshot()
                return SessionResumeResult(
                    self._writer.info,
                    ResumeEffect.ALREADY_CURRENT,
                    None,
                    snapshot.context_id,
                    False,
                    LatestUpdateStatus.UPDATED,
                )
            old = self._writer
            old_loop = self._loop
            old_sequence = old.state.next_sequence
            old_context_id = old_loop.effective_context_snapshot().context_id
            prepared = self._session_store.prepare_resume(selector)
            writer_holder: dict[str, SessionWriter] = {}
            try:
                loop = self._loop_from_state(
                    prepared.state,
                    self._read_file,
                    commit_turn=lambda turn: self._commit_turn(writer_holder["writer"], turn),
                )
                snapshot = loop.effective_context_snapshot()
                with self._manager.provider_for_context_transition() as runtime:
                    assessment = runtime.assess_context(snapshot.to_conversation_request())
                    report = assessment.fit_report
                    if report is not None and rejects_context_transition(report.decision):
                        raise SessionResumeContextError(prepared.info, snapshot.context_id, report)
                    if (
                        self._writer is not old
                        or self._loop is not old_loop
                        or old.state.next_sequence != old_sequence
                        or old_loop.effective_context_snapshot().context_id != old_context_id
                        or self._manager.status().generation != runtime.status.generation
                    ):
                        raise SessionResumeConflictError(
                            "current Session or runtime changed during resume screening"
                        )
                    committed = prepared.commit()
                writer_holder["writer"] = committed.writer
                self._writer = committed.writer
                self._loop = loop
                old.release()
                return _resume_result(
                    committed.writer.info,
                    snapshot.context_id,
                    assessment,
                    committed.recovery_applied,
                    committed.latest_status,
                    committed.latest_diagnostic,
                )
            except SessionResumeStaleError as error:
                raise SessionResumeConflictError(str(error)) from None
            finally:
                prepared.abort()

    def prompt(self, text: str) -> str:
        """Run one complete turn; transcript fsync succeeds before memory commit."""
        with self._lock:
            self._ensure_open()
            self._ensure_not_compacting()
            binding: BindingSnapshot | None = None
            try:
                with self._manager.provider_for_turn() as runtime:
                    binding = binding_from_status(runtime.status)
                    return self._loop.run(text, provider=runtime)
            except Exception as error:
                self._record_failure(binding or binding_from_status(self._manager.status()), error)
                raise

    def list_profiles(self) -> tuple[NamedProviderProfile, ...]:
        self._ensure_open()
        return self._store.list_profiles()

    def use_profile(self, name: str, *, scope: str = "project") -> RuntimeSwitchResult:
        with self._lock:
            self._ensure_open()
            self._ensure_not_compacting()
            result = self._manager.use_profile(
                name,
                scope=scope,
                committed_context=self._loop.effective_context_snapshot(),
            )
            self._record_runtime_switch(result, "provider_profile")
            return result

    def use_profile_id(self, profile_id: str, *, scope: str = "project") -> RuntimeSwitchResult:
        profile = self._store.get_profile_by_id(profile_id)
        return self.use_profile(profile.name, scope=scope)

    def clear_active(self, *, scope: str = "project") -> RuntimeSwitchResult:
        with self._lock:
            self._ensure_open()
            self._ensure_not_compacting()
            result = self._manager.clear_active(
                scope=scope,
                committed_context=self._loop.effective_context_snapshot(),
            )
            self._record_runtime_switch(result, "provider_clear")
            return result

    def set_model(self, model: str) -> RuntimeSwitchResult:
        with self._lock:
            self._ensure_open()
            self._ensure_not_compacting()
            result = self._manager.set_model(
                model,
                committed_context=self._loop.effective_context_snapshot(),
            )
            self._record_runtime_switch(result, "model_override")
            return result

    def compact_context(self) -> CompactContextResult:
        """Prepare, generate, validate, persist, then install one manual checkpoint."""
        with self._lock:
            self._ensure_open()
            if self._compaction_active:
                raise CompactionConflictError("a compaction transaction is already active")
            writer = self._writer
            loop = self._loop
            source = loop.effective_context_snapshot()
            effective_turns = source.effective_turns
            if len(effective_turns) < COMPACT_MIN_EFFECTIVE_TURNS:
                raise CompactionNotEligibleError(
                    f"controlled compaction requires at least "
                    f"{COMPACT_MIN_EFFECTIVE_TURNS} complete effective turns"
                )
            full_turns = source.full_turns
            summarized_turns = effective_turns[:-COMPACT_RETAINED_TURNS]
            retained_turns = effective_turns[-COMPACT_RETAINED_TURNS:]
            summarized_history = tuple(item for turn in summarized_turns for item in turn.items)
            retained_history = tuple(item for turn in retained_turns for item in turn.items)
            retained_from_full_turn = len(full_turns) - COMPACT_RETAINED_TURNS
            captured_sequence = writer.state.next_sequence
            captured_checkpoint = writer.state.latest_checkpoint
            captured_full = loop.history
            captured_effective = loop.effective_history
            captured_summary = loop.effective_summary
            captured_source = loop.effective_source
            self._compaction_active = True

        try:
            with self._manager.provider_for_compaction() as runtime:
                status = runtime.status
                output_limit = min(
                    COMPACT_MAX_OUTPUT_TOKENS,
                    status.max_output_tokens or COMPACT_MAX_OUTPUT_TOKENS,
                    status.model_max_output_tokens or COMPACT_MAX_OUTPUT_TOKENS,
                )
                source_report = runtime.assess_context(source.to_conversation_request())
                if source_report.decision == ContextFitDecision.MODEL_OUTPUT_EXCEEDED:
                    raise CompactionCandidateError(
                        "source context output reserve exceeds the model limit"
                    )
                before = source_report.input_count.input_tokens
                if source_report.decision == ContextFitDecision.UNKNOWN or before is None:
                    raise CompactionCandidateError(
                        "source context input count is unknown; compaction was not committed"
                    )
                summary_request = CompactSummaryRequest(
                    prompt=build_compact_prompt(),
                    source_text=build_compact_source_text(
                        previous_summary=captured_summary,
                        summarized_history=summarized_history,
                    ),
                    max_output_tokens=output_limit,
                )
                summary_response = runtime.summarize(summary_request)
                summary = EffectiveContextSummary(summary_response.text.strip())
                candidate = EffectiveContextSnapshot(
                    representation_version=COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
                    source=EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT,
                    system_prompt=source.system_prompt,
                    tool_definitions=source.tool_definitions,
                    full_history=source.full_history,
                    effective_history=retained_history,
                    effective_summary=summary,
                )
                candidate_report = runtime.assess_context(candidate.to_conversation_request())
                after = candidate_report.input_count.input_tokens
                if candidate_report.decision != ContextFitDecision.FITS or after is None:
                    raise CompactionCandidateError(
                        "candidate context compatibility is not a known fit; compaction was not committed"
                    )
                if source_report.input_count.method != candidate_report.input_count.method:
                    raise CompactionCandidateError(
                        "source and candidate input counts are not comparable"
                    )
                if after >= before:
                    raise CompactionCandidateError(
                        "candidate context did not reduce provider input tokens"
                    )

                with self._lock:
                    self._ensure_open()
                    current = loop.effective_context_snapshot()
                    if (
                        self._writer is not writer
                        or self._loop is not loop
                        or writer.state.next_sequence != captured_sequence
                        or loop.history != captured_full
                        or loop.effective_history != captured_effective
                        or loop.effective_summary != captured_summary
                        or loop.effective_source != captured_source
                        or current.context_id != source.context_id
                        or not self._compaction_active
                    ):
                        raise CompactionConflictError("compaction source changed; rerun /compact")
                    prompt = summary_request.prompt
                    checkpoint = ContextCompacted(
                        sequence=captured_sequence,
                        occurred_at=writer.now(),
                        binding=binding_from_status(status),
                        source_context_id=source.context_id,
                        result_context_id=candidate.context_id,
                        source_full_turn_count=len(full_turns),
                        source_effective_turn_count=len(effective_turns),
                        retained_from_full_turn=retained_from_full_turn,
                        previous_checkpoint_sequence=(
                            captured_checkpoint.sequence
                            if captured_checkpoint is not None
                            else None
                        ),
                        summary=summary.text,
                        compact_prompt_version=prompt.version,
                        compact_prompt_fingerprint=prompt.fingerprint,
                        continuation_version=summary.continuation_version,
                        continuation_fingerprint=summary.continuation_fingerprint,
                        effective_context_representation_version=(candidate.representation_version),
                    )
                    writer.append_context_compacted(checkpoint)
                    loop.install_compaction(summary=summary, retained_history=retained_history)
                return CompactContextResult(
                    session_id=writer.session_id,
                    checkpoint_sequence=checkpoint.sequence,
                    source_context_id=source.context_id,
                    result_context_id=candidate.context_id,
                    summarized_turn_count=len(summarized_turns),
                    retained_turn_count=len(retained_turns),
                    full_turn_count=len(full_turns),
                    before_input_tokens=before,
                    after_input_tokens=after,
                    input_method=candidate_report.input_count.method.value,
                    fit_decision=candidate_report.decision,
                )
        finally:
            with self._lock:
                self._compaction_active = False

    def inspect_context(self) -> EffectiveContextInspection:
        """Inspect current effective context without generation or durable mutation."""
        with self._lock:
            self._ensure_open()
            snapshot = self._loop.effective_context_snapshot()
            assessment = self._manager.assess_current_context(snapshot.to_conversation_request())
            return EffectiveContextInspection(
                snapshot,
                assessment,
                checkpoint=self._writer.state.latest_checkpoint,
            )

    def status(self) -> RuntimeStatus:
        self._ensure_open()
        return self._manager.status()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._ensure_not_compacting()
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

    @staticmethod
    def _loop_from_state(state, read_file, *, commit_turn) -> AgentLoop:
        return AgentLoop(
            None,
            read_file,
            initial_history=state.history,
            initial_effective_history=state.effective_history,
            initial_effective_summary=state.effective_summary,
            initial_effective_source=state.effective_source,
            commit_turn=commit_turn,
        )

    def _new_loop(self, writer: SessionWriter) -> AgentLoop:
        return self._loop_from_state(
            writer.state,
            self._read_file,
            commit_turn=lambda turn: self._commit_turn(writer, turn),
        )

    def _commit_turn(self, writer: SessionWriter, turn: CommittedTurn) -> None:
        if writer is not self._writer:
            raise SessionStoreError("conversation session changed before turn commit")
        writer.append_turn(turn.items, binding=binding_from_status(self._manager.status()))

    def _record_runtime_switch(self, result: RuntimeSwitchResult, reason: str) -> None:
        try:
            self._writer.runtime_changed(binding_from_status(result.status), reason=reason)
        except Exception as error:
            raise RuntimeSwitchAuditError(result) from error

    def _record_failure(self, binding: BindingSnapshot, error: Exception) -> None:
        try:
            self._writer.turn_failed(
                binding=binding,
                failure_kind=type(error).__name__,
                message=_safe_failure_message(error),
            )
        except SessionStoreError:
            pass

    def _ensure_not_compacting(self) -> None:
        if self._compaction_active:
            raise CompactionConflictError("a controlled compaction transaction is active")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("project session is closed")


def _resume_result(
    info: SessionInfo,
    context_id: str,
    assessment: CurrentTargetContextAssessment,
    recovery_applied: bool,
    latest_status: LatestUpdateStatus,
    diagnostic: str | None,
) -> SessionResumeResult:
    effect = ResumeEffect.APPLIED
    if latest_status == LatestUpdateStatus.FAILED_UNCHANGED:
        effect = ResumeEffect.APPLIED_LATEST_FAILED
    elif latest_status == LatestUpdateStatus.REPLACED_DURABILITY_UNKNOWN:
        effect = ResumeEffect.APPLIED_LATEST_DURABILITY_UNKNOWN
    return SessionResumeResult(
        info,
        effect,
        assessment,
        context_id,
        recovery_applied,
        latest_status,
        diagnostic,
    )


def _selector_matches_current(
    selector: str | Path,
    writer: SessionWriter,
    session_store: SessionStore,
) -> bool:
    if isinstance(selector, Path):
        candidate = selector if selector.is_absolute() else Path.cwd() / selector
        return candidate.absolute() == writer.path.absolute()
    if selector == writer.session_id:
        return True
    if selector == "latest":
        try:
            return session_store.show("latest").session_id == writer.session_id
        except SessionStoreError:
            return False
    return False


def binding_from_status(status: RuntimeStatus) -> BindingSnapshot:
    """Build non-secret per-turn provenance without influencing future runtime selection."""
    if status.mode == "fake":
        return BindingSnapshot.fake(
            generation=status.generation,
            source=status.selection_source,
        )
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
        generation=status.generation,
        adapter_version=f"route-contract-v{status.adapter_contract_version}",
        route_fingerprint=status.route_fingerprint,
    )


def _safe_failure_message(error: Exception) -> str:
    if isinstance(error, ContextPreflightError):
        return str(error)[:4096]
    if isinstance(error, ProviderAdapterError):
        return error.failure.message[:4096]
    if isinstance(error, SessionStoreError):
        return str(error)[:4096]
    return type(error).__name__
