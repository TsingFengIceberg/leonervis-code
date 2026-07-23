from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from leonervis_code.core.action_coordinator import (
    ActionCoordinator,
    ActionExecutionResult,
    ActionIdentityChangedError,
    ApprovalResolution,
)
from leonervis_code.core.actions import (
    ActionIdentity,
    ActionLease,
    ActionPrecondition,
)
from leonervis_code.core.approvals import ApprovalGrantError, ApprovalGrantRejection
from leonervis_code.core.contracts import ToolArguments, ToolResult
from leonervis_code.core.permissions import ApprovalMode, PermissionAction, PermissionMode
from leonervis_code.session_records import (
    ActionAuthorization,
    ActionExecutionOutcome,
    ApprovalAuditOutcome,
    BindingSnapshot,
)

SESSION_ID = "12345678-1234-4234-9234-123456789abc"
LEASE_ID = "22345678-1234-4234-9234-123456789abc"
REQUEST_ID = "32345678-1234-4234-9234-123456789abc"
GRANT_ID = "42345678-1234-4234-9234-123456789abc"
CONTEXT_ID = f"ctx-v1-{'1' * 64}"
WORKSPACE_FINGERPRINT = f"v1-{'2' * 64}"


def identity(action: PermissionAction = PermissionAction.WORKSPACE_CREATE) -> ActionIdentity:
    return ActionIdentity(
        request_id=REQUEST_ID,
        tool_use_id="tool-1",
        tool_name="write_file" if action != PermissionAction.WORKSPACE_READ else "read_file",
        arguments=ToolArguments.from_mapping(
            {"content": "hello\n", "path": "notes.txt"}
            if action != PermissionAction.WORKSPACE_READ
            else {"path": "notes.txt"}
        ),
        action=action,
        workspace_fingerprint=WORKSPACE_FINGERPRINT,
        lease=ActionLease(SESSION_ID, LEASE_ID, 0, CONTEXT_ID),
        precondition=(
            ActionPrecondition.none()
            if action == PermissionAction.WORKSPACE_READ
            else ActionPrecondition.path_absent()
        ),
    )


class RecordingWriter:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_at = fail_at

    def _record(self, name: str, *values) -> None:
        self.calls.append((name, *values))
        if self.fail_at == name:
            raise RuntimeError(f"injected {name} failure")

    def action_requested(self, **values) -> None:
        self._record("action_requested", values["identity"].digest)

    def permission_decided(self, **values) -> None:
        result = values["result"]
        self._record("permission_decided", result.decision.value, result.reason.value)

    def approval_resolved(self, **values) -> None:
        self._record("approval_resolved", values["outcome"], values["grant_id"])

    def action_execution_started(self, **values) -> None:
        self._record(
            "action_execution_started",
            values["authorization"],
            values["grant_id"],
        )

    def action_execution_finished(self, **values) -> None:
        self._record(
            "action_execution_finished",
            values["outcome"],
            values["result_code"],
        )


def execution(exact: ActionIdentity, *, error: bool = False) -> ActionExecutionResult:
    return ActionExecutionResult(
        ToolResult(exact.tool_use_id, "failed" if error else "ok", is_error=error),
        ActionExecutionOutcome.FAILED if error else ActionExecutionOutcome.SUCCEEDED,
        "tool_error" if error else "ok",
        "executor failed" if error else "executor succeeded",
    )


def coordinator(writer: RecordingWriter, approval=ApprovalResolution.ACCEPT) -> ActionCoordinator:
    return ActionCoordinator(
        writer=writer,  # type: ignore[arg-type]
        approval_handler=lambda _request: approval,
        uuid_factory=lambda: UUID(GRANT_ID),
    )


def test_policy_allow_durably_starts_before_executor_and_finishes_afterward() -> None:
    writer = RecordingWriter()
    exact = identity(PermissionAction.WORKSPACE_READ)
    order: list[str] = []

    def execute(current: ActionIdentity) -> ActionExecutionResult:
        assert writer.calls[-1][0] == "action_execution_started"
        order.append("execute")
        return execution(current)

    result = coordinator(writer).run(
        identity=exact,
        binding=BindingSnapshot.fake(),
        permission_mode=PermissionMode.READ_ONLY,
        approval_mode=ApprovalMode.ASK,
        revalidate=lambda current: current,
        execute=execute,
    )

    assert result.executed
    assert result.approval_resolution is None
    assert result.tool_result == ToolResult("tool-1", "ok")
    assert order == ["execute"]
    assert writer.calls == [
        ("action_requested", exact.digest),
        ("permission_decided", "allow", "allowed_workspace_read"),
        ("action_execution_started", ActionAuthorization.POLICY_ALLOW, None),
        ("action_execution_finished", ActionExecutionOutcome.SUCCEEDED, "ok"),
    ]


def test_policy_deny_never_prompts_revalidates_or_executes() -> None:
    writer = RecordingWriter()
    exact = identity()
    approval_calls = []
    coordinator_under_test = ActionCoordinator(
        writer=writer,  # type: ignore[arg-type]
        approval_handler=lambda request: approval_calls.append(request),  # type: ignore[arg-type,func-returns-value]
    )

    result = coordinator_under_test.run(
        identity=exact,
        binding=BindingSnapshot.fake(),
        permission_mode=PermissionMode.READ_ONLY,
        approval_mode=ApprovalMode.AUTO,
        revalidate=lambda _current: pytest.fail("deny must not revalidate"),
        execute=lambda _current: pytest.fail("deny must not execute"),
    )

    assert result.executed is False
    assert result.approval_resolution is None
    assert result.tool_result.is_error
    assert "denied_read_only_mode" in result.tool_result.content
    assert approval_calls == []
    assert [call[0] for call in writer.calls] == ["action_requested", "permission_decided"]


@pytest.mark.parametrize(
    ("resolution", "audit_outcome", "message"),
    [
        (ApprovalResolution.REJECT, ApprovalAuditOutcome.REJECTED, "rejected"),
        (ApprovalResolution.CANCEL, ApprovalAuditOutcome.CANCELLED, "cancelled"),
    ],
)
def test_ask_reject_or_cancel_is_terminal_without_execution(
    resolution: ApprovalResolution,
    audit_outcome: ApprovalAuditOutcome,
    message: str,
) -> None:
    writer = RecordingWriter()

    result = coordinator(writer, resolution).run(
        identity=identity(),
        binding=BindingSnapshot.fake(),
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        revalidate=lambda _current: pytest.fail("non-accept must not revalidate"),
        execute=lambda _current: pytest.fail("non-accept must not execute"),
    )

    assert result.executed is False
    assert result.approval_resolution == resolution
    assert result.tool_result.is_error
    assert message in result.tool_result.content
    assert writer.calls[-1] == ("approval_resolved", audit_outcome, None)


def test_ask_accept_issues_exact_grant_then_starts_with_that_grant() -> None:
    writer = RecordingWriter()
    exact = identity()

    result = coordinator(writer).run(
        identity=exact,
        binding=BindingSnapshot.fake(),
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        revalidate=lambda current: current,
        execute=lambda current: execution(current),
    )

    assert result.executed
    assert result.approval_resolution == ApprovalResolution.ACCEPT
    assert writer.calls[2] == (
        "approval_resolved",
        ApprovalAuditOutcome.ACCEPTED,
        GRANT_ID,
    )
    assert writer.calls[3] == (
        "action_execution_started",
        ActionAuthorization.APPROVAL_GRANT,
        GRANT_ID,
    )


def test_stale_auto_identity_rejects_before_durable_start_or_execution() -> None:
    writer = RecordingWriter()
    exact = identity()

    with pytest.raises(ActionIdentityChangedError, match="changed before execution"):
        coordinator(writer).run(
            identity=exact,
            binding=BindingSnapshot.fake(),
            permission_mode=PermissionMode.WORKSPACE_WRITE,
            approval_mode=ApprovalMode.AUTO,
            revalidate=lambda current: replace(
                current,
                precondition=ActionPrecondition.expected_state("3" * 64),
            ),
            execute=lambda _current: pytest.fail("stale action must not execute"),
        )

    assert [call[0] for call in writer.calls] == ["action_requested", "permission_decided"]


def test_stale_approved_precondition_rejects_exact_grant_before_start() -> None:
    writer = RecordingWriter()
    exact = identity()

    with pytest.raises(ApprovalGrantError) as caught:
        coordinator(writer).run(
            identity=exact,
            binding=BindingSnapshot.fake(),
            permission_mode=PermissionMode.WORKSPACE_WRITE,
            approval_mode=ApprovalMode.ASK,
            revalidate=lambda current: replace(
                current,
                precondition=ActionPrecondition.expected_state("3" * 64),
            ),
            execute=lambda _current: pytest.fail("stale action must not execute"),
        )

    assert caught.value.code == ApprovalGrantRejection.STALE_PRECONDITION
    assert [call[0] for call in writer.calls] == [
        "action_requested",
        "permission_decided",
        "approval_resolved",
    ]


def test_durable_start_failure_prevents_executor() -> None:
    writer = RecordingWriter(fail_at="action_execution_started")

    with pytest.raises(RuntimeError, match="injected action_execution_started"):
        coordinator(writer).run(
            identity=identity(PermissionAction.WORKSPACE_READ),
            binding=BindingSnapshot.fake(),
            permission_mode=PermissionMode.READ_ONLY,
            approval_mode=ApprovalMode.AUTO,
            revalidate=lambda current: current,
            execute=lambda _current: pytest.fail("executor must not run before durable start"),
        )


def test_executor_error_result_and_exception_both_finish_as_failed() -> None:
    for executor, expected_code in [
        (lambda current: execution(current, error=True), "tool_error"),
        (lambda _current: (_ for _ in ()).throw(OSError("secret")), "executor_error"),
    ]:
        writer = RecordingWriter()
        result = coordinator(writer).run(
            identity=identity(PermissionAction.WORKSPACE_READ),
            binding=BindingSnapshot.fake(),
            permission_mode=PermissionMode.READ_ONLY,
            approval_mode=ApprovalMode.AUTO,
            revalidate=lambda current: current,
            execute=executor,
        )

        assert result.executed
        assert result.tool_result.is_error
        assert writer.calls[-1] == (
            "action_execution_finished",
            ActionExecutionOutcome.FAILED,
            expected_code,
        )


def test_known_partial_executor_outcome_is_durably_distinct_from_failure() -> None:
    writer = RecordingWriter()

    result = coordinator(writer).run(
        identity=identity(PermissionAction.WORKSPACE_READ),
        binding=BindingSnapshot.fake(),
        permission_mode=PermissionMode.READ_ONLY,
        approval_mode=ApprovalMode.AUTO,
        revalidate=lambda current: current,
        execute=lambda current: ActionExecutionResult(
            ToolResult(current.tool_use_id, "visible but uncertain", is_error=True),
            ActionExecutionOutcome.PARTIAL,
            "durability_unknown",
            "visible effect with unknown durability",
        ),
    )

    assert result.tool_result.is_error
    assert writer.calls[-1] == (
        "action_execution_finished",
        ActionExecutionOutcome.PARTIAL,
        "durability_unknown",
    )


def test_finish_audit_failure_propagates_after_executor_has_run() -> None:
    writer = RecordingWriter(fail_at="action_execution_finished")
    executed: list[bool] = []

    def execute(current: ActionIdentity) -> ActionExecutionResult:
        executed.append(True)
        return execution(current)

    with pytest.raises(RuntimeError, match="injected action_execution_finished"):
        coordinator(writer).run(
            identity=identity(PermissionAction.WORKSPACE_READ),
            binding=BindingSnapshot.fake(),
            permission_mode=PermissionMode.READ_ONLY,
            approval_mode=ApprovalMode.AUTO,
            revalidate=lambda current: current,
            execute=execute,
        )

    assert executed == [True]
