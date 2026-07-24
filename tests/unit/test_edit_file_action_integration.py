from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from leonervis_code.cli.presentation import render_action_audits
from leonervis_code.core.action_coordinator import (
    ActionCoordinator,
    ActionExecutionResult,
    ApprovalResolution,
    HumanApprovalRequest,
)
from leonervis_code.core.actions import ActionIdentity, ActionLease
from leonervis_code.core.approvals import ApprovalGrantError, ApprovalGrantRejection
from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.core.permissions import ApprovalMode, PermissionMode
from leonervis_code.session_records import (
    ActionAuditStatus,
    ActionExecutionOutcome,
    BindingSnapshot,
)
from leonervis_code.session_store import SessionStore
from leonervis_code.tools.edit_file import EditFileOutcome, EditFileTool, PreparedEditFile

SESSION_ID = "12345678-1234-4234-9234-123456789abc"
REQUEST_ID = "22345678-1234-4234-9234-123456789abc"
LEASE_ID = "32345678-1234-4234-9234-123456789abc"
GRANT_ID = "42345678-1234-4234-9234-123456789abc"
CONTEXT_ID = f"ctx-v1-{'1' * 64}"
NOW = "2026-07-24T12:00:00.000000Z"


def edit_request(*, old_text: str = "before", new_text: str = "after") -> ToolUse:
    return ToolUse(
        "edit-1",
        "edit_file",
        ToolArguments.from_mapping(
            {"path": "note.txt", "old_text": old_text, "new_text": new_text}
        ),
    )


def action_identity(
    prepared: PreparedEditFile,
    store: SessionStore,
    session_id: str,
) -> ActionIdentity:
    return ActionIdentity(
        request_id=REQUEST_ID,
        tool_use_id=prepared.request.tool_use_id,
        tool_name=prepared.request.name,
        arguments=prepared.request.arguments,
        action=prepared.action,
        workspace_fingerprint=store.workspace_fingerprint,
        lease=ActionLease(session_id, LEASE_ID, 0, CONTEXT_ID),
        precondition=prepared.precondition,
    )


def execution(tool: EditFileTool, prepared: PreparedEditFile) -> ActionExecutionResult:
    result = tool.execute_detailed(prepared)
    return ActionExecutionResult(
        result.tool_result,
        {
            EditFileOutcome.SUCCEEDED: ActionExecutionOutcome.SUCCEEDED,
            EditFileOutcome.FAILED: ActionExecutionOutcome.FAILED,
            EditFileOutcome.PARTIAL: ActionExecutionOutcome.PARTIAL,
        }[result.outcome],
        result.result_code,
        result.audit_message,
    )


def open_action_test(tmp_path: Path):
    binding = BindingSnapshot.fake()
    store = SessionStore(
        tmp_path,
        uuid_factory=lambda: UUID(SESSION_ID),
        clock=lambda: NOW,
    )
    writer = store.create(binding)
    tool = EditFileTool(tmp_path)
    prepared = tool.prepare(edit_request())
    identity = action_identity(prepared, store, writer.session_id)
    return binding, store, writer, tool, prepared, identity


def run_action(
    *,
    binding: BindingSnapshot,
    writer,
    tool: EditFileTool,
    prepared: PreparedEditFile,
    identity: ActionIdentity,
    permission_mode: PermissionMode,
    approval_mode: ApprovalMode,
    approval_handler,
):
    coordinator = ActionCoordinator(
        writer=writer,
        approval_handler=approval_handler,
        uuid_factory=lambda: UUID(GRANT_ID),
    )
    return coordinator.run(
        identity=identity,
        binding=binding,
        permission_mode=permission_mode,
        approval_mode=approval_mode,
        revalidate=lambda current: replace(
            current,
            precondition=tool.refresh_precondition(prepared),
        ),
        execute=lambda _current: execution(tool, prepared),
    )


def test_read_only_denies_exact_edit_before_execution_and_replays_audit(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    binding, store, writer, tool, prepared, identity = open_action_test(tmp_path)

    result = run_action(
        binding=binding,
        writer=writer,
        tool=tool,
        prepared=prepared,
        identity=identity,
        permission_mode=PermissionMode.READ_ONLY,
        approval_mode=ApprovalMode.AUTO,
        approval_handler=lambda _request: pytest.fail("deny must not ask"),
    )

    assert not result.executed
    assert result.tool_result.is_error
    assert target.read_text(encoding="utf-8") == "before"
    writer.close()
    audit = store.action_audits(SESSION_ID)[-1]
    assert audit.status == ActionAuditStatus.DENIED
    assert audit.identity.action.value == "workspace-overwrite"


def test_ask_accept_edits_and_persists_redacted_success_audit(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    binding, store, writer, tool, prepared, identity = open_action_test(tmp_path)
    approvals: list[HumanApprovalRequest] = []

    def approve(request: HumanApprovalRequest) -> ApprovalResolution:
        approvals.append(request)
        assert target.read_text(encoding="utf-8") == "before"
        return ApprovalResolution.ACCEPT

    result = run_action(
        binding=binding,
        writer=writer,
        tool=tool,
        prepared=prepared,
        identity=identity,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=approve,
    )

    assert result.executed
    assert result.approval_resolution == ApprovalResolution.ACCEPT
    assert result.tool_result.content.endswith('"replacements":1}\n')
    assert target.read_text(encoding="utf-8") == "after"
    assert approvals[0].identity.arguments == prepared.request.arguments
    writer.close()
    audits = store.action_audits(SESSION_ID)
    assert audits[-1].status == ActionAuditStatus.SUCCEEDED
    assert audits[-1].execution_outcome == ActionExecutionOutcome.SUCCEEDED
    assert audits[-1].result_code == "edited"
    rendered = render_action_audits(audits, 20)
    assert "Action #1: edit_file" in rendered
    assert "class: workspace-overwrite" in rendered
    assert "path: 'note.txt'" in rendered
    assert "result: succeeded (edited)" in rendered
    assert "before" not in rendered
    assert "after" not in rendered


@pytest.mark.parametrize(
    ("resolution", "status"),
    [
        (ApprovalResolution.REJECT, ActionAuditStatus.REJECTED),
        (ApprovalResolution.CANCEL, ActionAuditStatus.CANCELLED),
    ],
)
def test_reject_or_cancel_keeps_source_and_persists_terminal_audit(
    tmp_path: Path,
    resolution: ApprovalResolution,
    status: ActionAuditStatus,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    binding, store, writer, tool, prepared, identity = open_action_test(tmp_path)

    result = run_action(
        binding=binding,
        writer=writer,
        tool=tool,
        prepared=prepared,
        identity=identity,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=lambda _request: resolution,
    )

    assert not result.executed
    assert target.read_text(encoding="utf-8") == "before"
    writer.close()
    assert store.action_audits(SESSION_ID)[-1].status == status


def test_auto_allow_edits_without_calling_approval(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    binding, store, writer, tool, prepared, identity = open_action_test(tmp_path)

    result = run_action(
        binding=binding,
        writer=writer,
        tool=tool,
        prepared=prepared,
        identity=identity,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
        approval_handler=lambda _request: pytest.fail("auto must not ask"),
    )

    assert result.executed
    assert result.approval_resolution is None
    assert target.read_text(encoding="utf-8") == "after"
    writer.close()
    audit = store.action_audits(SESSION_ID)[-1]
    assert audit.status == ActionAuditStatus.SUCCEEDED
    assert audit.approval_outcome is None


def test_accepted_edit_becomes_stale_if_source_changes_during_approval(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    binding, store, writer, tool, prepared, identity = open_action_test(tmp_path)

    def mutate_then_accept(_request: HumanApprovalRequest) -> ApprovalResolution:
        target.write_text("external", encoding="utf-8")
        return ApprovalResolution.ACCEPT

    with pytest.raises(ApprovalGrantError) as caught:
        run_action(
            binding=binding,
            writer=writer,
            tool=tool,
            prepared=prepared,
            identity=identity,
            permission_mode=PermissionMode.WORKSPACE_WRITE,
            approval_mode=ApprovalMode.ASK,
            approval_handler=mutate_then_accept,
        )

    assert caught.value.code == ApprovalGrantRejection.STALE_PRECONDITION
    assert target.read_text(encoding="utf-8") == "external"
    writer.turn_failed(
        binding=binding,
        failure_kind="stale_action",
        message="approved edit became stale before execution",
    )
    writer.close(reason="error")
    audit = store.action_audits(SESSION_ID)[-1]
    assert audit.status == ActionAuditStatus.ABANDONED
    assert audit.execution_outcome is None
