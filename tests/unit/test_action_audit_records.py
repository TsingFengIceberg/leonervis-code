from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from uuid import UUID

import pytest

from leonervis_code.core.actions import (
    ActionIdentity,
    ActionLease,
    ActionPrecondition,
)
from leonervis_code.core.compaction import EffectiveContextSummary, build_compact_prompt
from leonervis_code.core.contracts import AssistantText, ToolArguments, UserMessage
from leonervis_code.core.permissions import (
    ApprovalMode,
    PermissionAction,
    PermissionDecision,
    PermissionGate,
    PermissionMode,
    PermissionReason,
    PermissionRequest,
)
from leonervis_code.session_records import (
    ActionAuditStatus,
    ActionAuthorization,
    ActionExecutionFinished,
    ActionExecutionOutcome,
    ActionExecutionStarted,
    ActionRequested,
    ApprovalAuditOutcome,
    ApprovalResolved,
    BindingSnapshot,
    ContextCompacted,
    PermissionDecided,
    RuntimeChanged,
    SessionClosed,
    SessionHeader,
    SessionRecordError,
    SessionResumed,
    TURN_COMMITTED_LEGACY_SCHEMA_VERSION,
    TurnCommitted,
    TurnFailed,
    decode_record,
    encode_record,
    replay_records,
    workspace_fingerprint,
)
from leonervis_code.session_store import (
    ActionOutcomeAuditError,
    SessionStore,
)

SESSION_ID = "12345678-1234-4234-9234-123456789abc"
LEASE_ID = "22345678-1234-4234-9234-123456789abc"
REQUEST_ID = "32345678-1234-4234-9234-123456789abc"
SECOND_REQUEST_ID = "42345678-1234-4234-9234-123456789abc"
GRANT_ID = "52345678-1234-4234-9234-123456789abc"
SECOND_GRANT_ID = "62345678-1234-4234-9234-123456789abc"
NOW = "2026-07-23T12:00:00.000000Z"
CONTEXT_ID = f"ctx-v1-{'1' * 64}"


def header(workspace: Path, *, binding: BindingSnapshot | None = None) -> SessionHeader:
    return SessionHeader(
        sequence=0,
        session_id=SESSION_ID,
        workspace=str(workspace.resolve()),
        workspace_fingerprint=workspace_fingerprint(workspace),
        created_at=NOW,
        binding=binding or BindingSnapshot.fake(),
    )


def identity(
    workspace: Path,
    *,
    request_id: str = REQUEST_ID,
    action: PermissionAction = PermissionAction.WORKSPACE_CREATE,
    runtime_generation: int = 0,
    lease_id: str = LEASE_ID,
    precondition: ActionPrecondition | None = None,
) -> ActionIdentity:
    return ActionIdentity(
        request_id=request_id,
        tool_use_id=f"tool-{request_id[0]}",
        tool_name="write_file",
        arguments=ToolArguments.from_mapping(
            {"content": "hello\n", "path": f"notes-{request_id[0]}.txt"}
        ),
        action=action,
        workspace_fingerprint=workspace_fingerprint(workspace),
        lease=ActionLease(
            session_id=SESSION_ID,
            lease_id=lease_id,
            runtime_generation=runtime_generation,
            context_id=CONTEXT_ID,
        ),
        precondition=precondition or ActionPrecondition.path_absent(),
    )


def request_record(
    workspace: Path,
    *,
    sequence: int = 1,
    exact: ActionIdentity | None = None,
    binding: BindingSnapshot | None = None,
    permission_mode: PermissionMode = PermissionMode.WORKSPACE_WRITE,
    approval_mode: ApprovalMode = ApprovalMode.AUTO,
) -> ActionRequested:
    return ActionRequested(
        sequence=sequence,
        occurred_at=NOW,
        binding=binding or BindingSnapshot.fake(),
        identity=exact or identity(workspace),
        permission_mode=permission_mode,
        approval_mode=approval_mode,
    )


def decision_record(
    exact: ActionIdentity,
    *,
    sequence: int = 2,
    permission_mode: PermissionMode = PermissionMode.WORKSPACE_WRITE,
    approval_mode: ApprovalMode = ApprovalMode.AUTO,
) -> PermissionDecided:
    result = PermissionGate().evaluate(
        PermissionRequest(permission_mode, approval_mode, exact.action)
    )
    return PermissionDecided(
        sequence=sequence,
        occurred_at=NOW,
        action_request_id=exact.request_id,
        action_digest=exact.digest,
        decision=result.decision,
        reason=result.reason,
    )


def start_record(
    exact: ActionIdentity,
    *,
    sequence: int,
    authorization: ActionAuthorization,
    grant_id: str | None = None,
) -> ActionExecutionStarted:
    return ActionExecutionStarted(
        sequence=sequence,
        occurred_at=NOW,
        action_request_id=exact.request_id,
        action_digest=exact.digest,
        authorization=authorization,
        grant_id=grant_id,
    )


def finish_record(
    exact: ActionIdentity,
    *,
    sequence: int,
    outcome: ActionExecutionOutcome = ActionExecutionOutcome.SUCCEEDED,
    result_code: str | None = None,
    message: str | None = None,
) -> ActionExecutionFinished:
    if result_code is None:
        result_code = "ok" if outcome == ActionExecutionOutcome.SUCCEEDED else "io_error"
    if message is None:
        message = "written" if outcome == ActionExecutionOutcome.SUCCEEDED else "write failed"
    return ActionExecutionFinished(
        sequence=sequence,
        occurred_at=NOW,
        action_request_id=exact.request_id,
        action_digest=exact.digest,
        outcome=outcome,
        result_code=result_code,
        message=message,
    )


def test_action_audit_record_codec_round_trips_canonical_closed_records(
    tmp_path: Path,
) -> None:
    exact = identity(tmp_path)
    records = (
        request_record(tmp_path, exact=exact),
        decision_record(exact),
        ApprovalResolved(
            sequence=3,
            occurred_at=NOW,
            action_request_id=exact.request_id,
            action_digest=exact.digest,
            outcome=ApprovalAuditOutcome.ACCEPTED,
            grant_id=GRANT_ID,
        ),
        start_record(
            exact,
            sequence=4,
            authorization=ActionAuthorization.APPROVAL_GRANT,
            grant_id=GRANT_ID,
        ),
        finish_record(exact, sequence=5),
    )

    for record in records:
        encoded = encode_record(record)
        assert decode_record(encoded) == record
        assert encode_record(decode_record(encoded)) == encoded


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value.update(extra=True), "unknown field"),
        (lambda value: value.pop("identity"), "missing required field"),
        (lambda value: value.update(permission_mode="root"), "permission_mode is invalid"),
        (lambda value: value["identity"].update(extra=True), "identity fields are invalid"),
    ],
)
def test_action_requested_decoder_fails_closed(tmp_path: Path, mutate, match: str) -> None:
    value = json.loads(encode_record(request_record(tmp_path)))
    mutate(value)

    with pytest.raises(SessionRecordError, match=match):
        decode_record(json.dumps(value).encode())


@pytest.mark.parametrize(
    "record,field,bad_value,match",
    [
        (
            lambda exact: decision_record(exact),
            "decision",
            "maybe",
            "decision is invalid",
        ),
        (
            lambda exact: ApprovalResolved(
                3,
                NOW,
                exact.request_id,
                exact.digest,
                ApprovalAuditOutcome.ACCEPTED,
                GRANT_ID,
            ),
            "outcome",
            "maybe",
            "outcome is invalid",
        ),
        (
            lambda exact: start_record(
                exact,
                sequence=4,
                authorization=ActionAuthorization.APPROVAL_GRANT,
                grant_id=GRANT_ID,
            ),
            "authorization",
            "maybe",
            "authorization is invalid",
        ),
        (
            lambda exact: finish_record(exact, sequence=5),
            "outcome",
            "maybe",
            "outcome is invalid",
        ),
    ],
)
def test_action_reference_record_decoders_reject_unknown_enums(
    tmp_path: Path, record, field: str, bad_value: object, match: str
) -> None:
    value = json.loads(encode_record(record(identity(tmp_path))))
    value[field] = bad_value

    with pytest.raises(SessionRecordError, match=match):
        decode_record(json.dumps(value).encode())


def test_replay_allow_start_success_is_audit_only(tmp_path: Path) -> None:
    exact = identity(tmp_path)
    records = [
        header(tmp_path),
        request_record(tmp_path, exact=exact),
        decision_record(exact),
        start_record(exact, sequence=3, authorization=ActionAuthorization.POLICY_ALLOW),
        finish_record(exact, sequence=4),
    ]

    state = replay_records(records)

    assert state.history == ()
    assert state.effective_history == ()
    assert state.action_audits[0].status == ActionAuditStatus.SUCCEEDED
    assert state.action_audits[0].execution_outcome == ActionExecutionOutcome.SUCCEEDED
    assert state.action_audits[0].result_code == "ok"


def test_replay_ask_accept_grant_start_failure(tmp_path: Path) -> None:
    exact = identity(tmp_path, action=PermissionAction.WORKSPACE_OVERWRITE)
    records = [
        header(tmp_path),
        request_record(
            tmp_path,
            exact=exact,
            approval_mode=ApprovalMode.ASK,
        ),
        decision_record(exact, approval_mode=ApprovalMode.ASK),
        ApprovalResolved(
            3,
            NOW,
            exact.request_id,
            exact.digest,
            ApprovalAuditOutcome.ACCEPTED,
            GRANT_ID,
        ),
        start_record(
            exact,
            sequence=4,
            authorization=ActionAuthorization.APPROVAL_GRANT,
            grant_id=GRANT_ID,
        ),
        finish_record(exact, sequence=5, outcome=ActionExecutionOutcome.FAILED),
    ]

    state = replay_records(records)

    audit = state.action_audits[0]
    assert audit.status == ActionAuditStatus.FAILED
    assert audit.approval_outcome == ApprovalAuditOutcome.ACCEPTED
    assert audit.grant_id == GRANT_ID
    assert audit.execution_outcome == ActionExecutionOutcome.FAILED
    assert audit.result_code == "io_error"


def test_partial_execution_round_trips_and_terminates_the_action_lifecycle(
    tmp_path: Path,
) -> None:
    exact = identity(tmp_path)
    partial = finish_record(
        exact,
        sequence=4,
        outcome=ActionExecutionOutcome.PARTIAL,
        result_code="overwritten_durability_unknown",
        message="target is visible but directory durability is unknown",
    )
    records = [
        header(tmp_path),
        request_record(tmp_path, exact=exact),
        decision_record(exact),
        start_record(exact, sequence=3, authorization=ActionAuthorization.POLICY_ALLOW),
        partial,
        TurnCommitted(
            sequence=5,
            committed_at=NOW,
            binding=BindingSnapshot.fake(),
            items=(UserMessage("write"), AssistantText("inspect the target")),
        ),
    ]

    assert decode_record(encode_record(partial)) == partial
    state = replay_records(records)
    audit = state.action_audits[0]
    assert audit.status == ActionAuditStatus.PARTIAL
    assert audit.execution_outcome == ActionExecutionOutcome.PARTIAL
    assert audit.result_code == "overwritten_durability_unknown"
    assert audit.message == "target is visible but directory durability is unknown"
    assert state.history == (UserMessage("write"), AssistantText("inspect the target"))


@pytest.mark.parametrize(
    "permission_mode,approval_mode,approval_outcome,expected",
    [
        (PermissionMode.READ_ONLY, ApprovalMode.AUTO, None, ActionAuditStatus.DENIED),
        (
            PermissionMode.WORKSPACE_WRITE,
            ApprovalMode.ASK,
            ApprovalAuditOutcome.REJECTED,
            ActionAuditStatus.REJECTED,
        ),
        (
            PermissionMode.WORKSPACE_WRITE,
            ApprovalMode.ASK,
            ApprovalAuditOutcome.CANCELLED,
            ActionAuditStatus.CANCELLED,
        ),
    ],
)
def test_replay_terminal_non_execution_paths(
    tmp_path: Path,
    permission_mode: PermissionMode,
    approval_mode: ApprovalMode,
    approval_outcome: ApprovalAuditOutcome | None,
    expected: ActionAuditStatus,
) -> None:
    exact = identity(tmp_path)
    records = [
        header(tmp_path),
        request_record(
            tmp_path,
            exact=exact,
            permission_mode=permission_mode,
            approval_mode=approval_mode,
        ),
        decision_record(
            exact,
            permission_mode=permission_mode,
            approval_mode=approval_mode,
        ),
    ]
    if approval_outcome is not None:
        records.append(
            ApprovalResolved(
                3,
                NOW,
                exact.request_id,
                exact.digest,
                approval_outcome,
                None,
            )
        )

    assert replay_records(records).action_audits[0].status == expected


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda records, exact: records.__setitem__(
                2,
                replace(records[2], action_request_id=SECOND_REQUEST_ID),
            ),
            "unknown action",
        ),
        (
            lambda records, exact: records.__setitem__(
                2,
                replace(records[2], action_digest=f"act-v1-{'0' * 64}"),
            ),
            "digest does not match",
        ),
        (
            lambda records, exact: records.__setitem__(
                2,
                replace(
                    records[2],
                    decision=PermissionDecision.DENY,
                    reason=PermissionReason.DENIED_READ_ONLY_MODE,
                ),
            ),
            "deterministic policy",
        ),
    ],
)
def test_replay_rejects_mismatched_action_references_and_policy(
    tmp_path: Path, mutate, match: str
) -> None:
    exact = identity(tmp_path)
    records = [
        header(tmp_path),
        request_record(tmp_path, exact=exact),
        decision_record(exact),
    ]
    mutate(records, exact)

    with pytest.raises(SessionRecordError, match=match):
        replay_records(records)


@pytest.mark.parametrize(
    "approval,start,match",
    [
        (
            ApprovalResolved(
                3,
                NOW,
                REQUEST_ID,
                "PLACEHOLDER",
                ApprovalAuditOutcome.ACCEPTED,
                None,
            ),
            None,
            "requires a grant ID",
        ),
        (
            ApprovalResolved(
                3,
                NOW,
                REQUEST_ID,
                "PLACEHOLDER",
                ApprovalAuditOutcome.REJECTED,
                GRANT_ID,
            ),
            None,
            "must not contain a grant ID",
        ),
        (
            ApprovalResolved(
                3,
                NOW,
                REQUEST_ID,
                "PLACEHOLDER",
                ApprovalAuditOutcome.ACCEPTED,
                GRANT_ID,
            ),
            ActionExecutionStarted(
                4,
                NOW,
                REQUEST_ID,
                "PLACEHOLDER",
                ActionAuthorization.POLICY_ALLOW,
                None,
            ),
            "exact approval grant",
        ),
    ],
)
def test_replay_rejects_invalid_approval_and_authorization_shapes(
    tmp_path: Path,
    approval: ApprovalResolved,
    start: ActionExecutionStarted | None,
    match: str,
) -> None:
    exact = identity(tmp_path)
    approval = replace(approval, action_digest=exact.digest)
    records = [
        header(tmp_path),
        request_record(tmp_path, exact=exact, approval_mode=ApprovalMode.ASK),
        decision_record(exact, approval_mode=ApprovalMode.ASK),
        approval,
    ]
    if start is not None:
        records.append(replace(start, action_digest=exact.digest))

    with pytest.raises(SessionRecordError, match=match):
        replay_records(records)


def test_replay_rejects_duplicate_request_and_grant_ids(tmp_path: Path) -> None:
    first = identity(tmp_path)
    second_same_request = identity(tmp_path, lease_id=SECOND_GRANT_ID)
    first_records = [
        header(tmp_path),
        request_record(tmp_path, exact=first),
        decision_record(first),
        start_record(first, sequence=3, authorization=ActionAuthorization.POLICY_ALLOW),
        finish_record(first, sequence=4),
        request_record(tmp_path, sequence=5, exact=second_same_request),
    ]
    with pytest.raises(SessionRecordError, match="request ID is duplicated"):
        replay_records(first_records)

    second = identity(
        tmp_path,
        request_id=SECOND_REQUEST_ID,
        lease_id="72345678-1234-4234-9234-123456789abc",
    )
    records = [
        header(tmp_path),
        request_record(tmp_path, exact=first, approval_mode=ApprovalMode.ASK),
        decision_record(first, approval_mode=ApprovalMode.ASK),
        ApprovalResolved(
            3,
            NOW,
            first.request_id,
            first.digest,
            ApprovalAuditOutcome.ACCEPTED,
            GRANT_ID,
        ),
        start_record(
            first,
            sequence=4,
            authorization=ActionAuthorization.APPROVAL_GRANT,
            grant_id=GRANT_ID,
        ),
        finish_record(first, sequence=5),
        request_record(
            tmp_path,
            sequence=6,
            exact=second,
            approval_mode=ApprovalMode.ASK,
        ),
        decision_record(second, sequence=7, approval_mode=ApprovalMode.ASK),
        ApprovalResolved(
            8,
            NOW,
            second.request_id,
            second.digest,
            ApprovalAuditOutcome.ACCEPTED,
            GRANT_ID,
        ),
    ]
    with pytest.raises(SessionRecordError, match="grant ID is duplicated"):
        replay_records(records)


@pytest.mark.parametrize(
    "changed,match",
    [
        ("session", "Session does not match"),
        ("workspace", "workspace does not match"),
        ("runtime", "runtime generation is stale"),
        ("binding", "binding does not match"),
    ],
)
def test_action_request_is_bound_to_session_workspace_and_runtime(
    tmp_path: Path, changed: str, match: str
) -> None:
    binding = BindingSnapshot.fake(generation=2)
    exact = identity(tmp_path, runtime_generation=2)
    record = request_record(tmp_path, exact=exact, binding=binding)
    if changed == "session":
        record = replace(
            record,
            identity=replace(
                exact,
                lease=replace(
                    exact.lease,
                    session_id="82345678-1234-4234-9234-123456789abc",
                ),
            ),
        )
    elif changed == "workspace":
        record = replace(
            record,
            identity=replace(exact, workspace_fingerprint=f"v1-{'9' * 64}"),
        )
    elif changed == "runtime":
        record = replace(
            record, identity=replace(exact, lease=replace(exact.lease, runtime_generation=3))
        )
    else:
        record = replace(record, binding=BindingSnapshot.fake(generation=1))

    with pytest.raises(SessionRecordError, match=match):
        replay_records([header(tmp_path, binding=binding), record])


def test_only_one_unresolved_action_and_no_crossing_state_boundaries(tmp_path: Path) -> None:
    exact = identity(tmp_path)
    unresolved = [header(tmp_path), request_record(tmp_path, exact=exact)]
    second = identity(
        tmp_path,
        request_id=SECOND_REQUEST_ID,
        lease_id="72345678-1234-4234-9234-123456789abc",
    )
    with pytest.raises(SessionRecordError, match="unresolved action"):
        replay_records([*unresolved, request_record(tmp_path, sequence=2, exact=second)])

    prompt = build_compact_prompt()
    summary = EffectiveContextSummary("summary")
    boundaries = (
        RuntimeChanged(2, NOW, BindingSnapshot.fake(generation=1), "switch"),
        TurnCommitted(
            2,
            NOW,
            BindingSnapshot.fake(),
            (UserMessage("u"), AssistantText("a")),
        ),
        ContextCompacted(
            sequence=2,
            occurred_at=NOW,
            binding=BindingSnapshot.fake(),
            source_context_id=f"ctx-v1-{'1' * 64}",
            result_context_id=f"ctx-v2-{'2' * 64}",
            source_full_turn_count=4,
            source_effective_turn_count=4,
            retained_from_full_turn=2,
            previous_checkpoint_sequence=None,
            summary=summary.text,
            compact_prompt_version=prompt.version,
            compact_prompt_fingerprint=prompt.fingerprint,
            continuation_version=summary.continuation_version,
            continuation_fingerprint=summary.continuation_fingerprint,
            effective_context_representation_version=2,
        ),
        SessionClosed(2, NOW, "done"),
    )
    for boundary in boundaries:
        with pytest.raises(SessionRecordError, match="unresolved action"):
            replay_records([*unresolved, boundary])


def test_resume_and_turn_failure_derive_abandoned_or_outcome_unknown(tmp_path: Path) -> None:
    exact = identity(tmp_path)
    requested = [header(tmp_path), request_record(tmp_path, exact=exact)]
    abandoned = replay_records([*requested, SessionResumed(2, NOW)])
    assert abandoned.action_audits[0].status == ActionAuditStatus.ABANDONED

    started = [
        *requested,
        decision_record(exact),
        start_record(exact, sequence=3, authorization=ActionAuthorization.POLICY_ALLOW),
    ]
    unknown = replay_records([*started, SessionResumed(4, NOW)])
    assert unknown.action_audits[0].status == ActionAuditStatus.OUTCOME_UNKNOWN

    failed_turn = replay_records(
        [
            *started,
            TurnFailed(4, NOW, BindingSnapshot.fake(), "cancelled", "stopped"),
        ]
    )
    assert failed_turn.action_audits[0].status == ActionAuditStatus.OUTCOME_UNKNOWN


def test_action_audit_coexists_with_legacy_turn_without_entering_effective_context(
    tmp_path: Path,
) -> None:
    exact = identity(tmp_path)
    legacy = TurnCommitted(
        sequence=1,
        committed_at=NOW,
        binding=BindingSnapshot.fake(),
        items=(UserMessage("legacy"), AssistantText("answer")),
        schema_version=TURN_COMMITTED_LEGACY_SCHEMA_VERSION,
    )
    records = [
        header(tmp_path),
        legacy,
        request_record(tmp_path, sequence=2, exact=exact),
        decision_record(exact, sequence=3),
        start_record(exact, sequence=4, authorization=ActionAuthorization.POLICY_ALLOW),
        finish_record(exact, sequence=5),
    ]

    decoded = [decode_record(encode_record(record)) for record in records]
    state = replay_records(decoded)

    assert state.history == legacy.items
    assert state.effective_history == legacy.items
    assert len(state.action_audits) == 1
    assert encode_record(decoded[1]) == encode_record(legacy)


def test_session_writer_appends_reopens_and_derives_interrupted_action(
    tmp_path: Path,
) -> None:
    session_store = SessionStore(
        tmp_path,
        uuid_factory=lambda: UUID(SESSION_ID),
        clock=lambda: NOW,
    )
    binding = BindingSnapshot.fake()
    exact = identity(tmp_path)
    writer = session_store.create(binding)
    writer.action_requested(
        identity=exact,
        binding=binding,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
    )
    result = PermissionGate().evaluate(
        PermissionRequest(
            PermissionMode.WORKSPACE_WRITE,
            ApprovalMode.AUTO,
            exact.action,
        )
    )
    writer.permission_decided(identity=exact, result=result)
    writer.action_execution_started(
        identity=exact,
        authorization=ActionAuthorization.POLICY_ALLOW,
        grant_id=None,
    )
    writer.release()

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["record_type"] for line in lines[-3:]] == [
        "action_requested",
        "permission_decided",
        "action_execution_started",
    ]

    resumed = session_store.open(SESSION_ID)
    assert resumed.state.action_audits[0].status == ActionAuditStatus.OUTCOME_UNKNOWN
    resumed.release()


def test_finish_audit_failure_reports_known_outcome_without_retrying_effect(
    monkeypatch, tmp_path: Path
) -> None:
    session_store = SessionStore(
        tmp_path,
        uuid_factory=lambda: UUID(SESSION_ID),
        clock=lambda: NOW,
    )
    binding = BindingSnapshot.fake()
    exact = identity(tmp_path)
    writer = session_store.create(binding)
    writer.action_requested(
        identity=exact,
        binding=binding,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
    )
    result = PermissionGate().evaluate(
        PermissionRequest(
            PermissionMode.WORKSPACE_WRITE,
            ApprovalMode.AUTO,
            exact.action,
        )
    )
    writer.permission_decided(identity=exact, result=result)
    writer.action_execution_started(
        identity=exact,
        authorization=ActionAuthorization.POLICY_ALLOW,
        grant_id=None,
    )

    import leonervis_code.session_store as session_store_module

    def fail_append(*args, **kwargs):
        raise OSError("fsync failed")

    monkeypatch.setattr(session_store_module, "_append_record_descriptor", fail_append)

    with pytest.raises(ActionOutcomeAuditError) as captured:
        writer.action_execution_finished(
            identity=exact,
            outcome=ActionExecutionOutcome.SUCCEEDED,
            result_code="ok",
            message="already written",
        )

    assert captured.value.action_request_id == exact.request_id
    assert captured.value.action_digest == exact.digest
    assert captured.value.execution_outcome == ActionExecutionOutcome.SUCCEEDED
    assert captured.value.result_code == "ok"
    assert writer.state.action_audits[0].status == ActionAuditStatus.EXECUTING
    assert isinstance(captured.value.__cause__, OSError)
    writer.release()
