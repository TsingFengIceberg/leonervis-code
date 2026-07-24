from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from leonervis_code.core.action_coordinator import ApprovalResolution, HumanApprovalRequest
from leonervis_code.core.approvals import ApprovalGrantError, ApprovalGrantRejection
from leonervis_code.core.contracts import (
    AssistantText,
    ToolArguments,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.core.permissions import ApprovalMode, PermissionMode
from leonervis_code.providers.request_context import RequestTokenCount, RequestTokenCountMethod
from leonervis_code.session import ProjectSession
from leonervis_code.session_records import ActionAuditStatus, ActionExecutionOutcome
from leonervis_code.session_store import SessionStore, SessionStoreError

SESSION_ID = "12345678-1234-4234-9234-123456789abc"
NOW = "2026-07-23T12:00:00.000000Z"


class ToolProvider:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.requests = []

    def count_input_tokens(self, _request):
        return RequestTokenCount(100, RequestTokenCountMethod.ESTIMATED)

    def respond(self, request):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def write_call(
    path: str = "note.txt", content: str = "hello\n", *, tool_use_id: str = "write-1"
) -> ToolUse:
    return ToolUse(
        tool_use_id,
        "write_file",
        ToolArguments.from_mapping({"path": path, "content": content}),
    )


def session_store_factory(workspace: Path) -> SessionStore:
    return SessionStore(
        workspace,
        uuid_factory=lambda: UUID(SESSION_ID),
        clock=lambda: NOW,
    )


def uuid_factory():
    values = iter(
        [
            "22345678-1234-4234-9234-123456789abc",
            "32345678-1234-4234-9234-123456789abc",
            "42345678-1234-4234-9234-123456789abc",
            "52345678-1234-4234-9234-123456789abc",
            "62345678-1234-4234-9234-123456789abc",
            "72345678-1234-4234-9234-123456789abc",
        ]
    )
    return lambda: UUID(next(values))


def open_session(
    workspace: Path,
    provider: ToolProvider,
    *,
    permission_mode: PermissionMode = PermissionMode.READ_ONLY,
    approval_mode: ApprovalMode = ApprovalMode.ASK,
    approval_handler=None,
) -> ProjectSession:
    return ProjectSession.open(
        workspace,
        model="custom/model",
        custom_protocol="openai-compatible",
        custom_base_url="http://127.0.0.1:11434/v1",
        environment={},
        provider_factory=lambda route, *, environment: provider,
        user_profile_path=workspace / "user.json",
        project_profile_path=workspace / "project.json",
        session_store_factory=session_store_factory,
        permission_mode=permission_mode,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        action_uuid_factory=uuid_factory(),
    )


def test_default_read_only_denial_is_model_visible_audited_and_committed(tmp_path: Path) -> None:
    call = write_call()
    provider = ToolProvider([call, AssistantText("not written")])
    session = open_session(tmp_path, provider)
    try:
        assert session.prompt("write a note") == "not written"

        denied = ToolResult(
            "write-1",
            "permission denied: denied_read_only_mode",
            is_error=True,
        )
        assert provider.requests[1].history[-2:] == (call, denied)
        assert session.history == (
            UserMessage("write a note"),
            call,
            denied,
            AssistantText("not written"),
        )
        assert not (tmp_path / "note.txt").exists()
        audit = session.action_audits()[-1]
        assert audit.status == ActionAuditStatus.DENIED
        assert audit.execution_outcome is None
    finally:
        session.close()


def test_hard_rejected_write_returns_tool_error_without_action_audit(
    tmp_path: Path,
) -> None:
    call = write_call(path="nested//note.txt")
    provider = ToolProvider([call, AssistantText("invalid path")])
    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
    )
    try:
        assert session.prompt("write an invalid path") == "invalid path"

        rejected = ToolResult(
            "write-1",
            "write_file path must be a portable workspace-relative file path",
            is_error=True,
        )
        assert provider.requests[1].history[-2:] == (call, rejected)
        assert session.history == (
            UserMessage("write an invalid path"),
            call,
            rejected,
            AssistantText("invalid path"),
        )
        assert session._writer.state.action_audits == ()
        assert not (tmp_path / "nested").exists()
    finally:
        session.close()


def test_workspace_write_ask_accept_creates_and_commits_exact_causality(tmp_path: Path) -> None:
    call = write_call(content="approved\n")
    provider = ToolProvider([call, AssistantText("created")])
    approval_requests: list[HumanApprovalRequest] = []

    def approve(request: HumanApprovalRequest) -> ApprovalResolution:
        approval_requests.append(request)
        assert not (tmp_path / "note.txt").exists()
        return ApprovalResolution.ACCEPT

    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=approve,
    )
    try:
        assert session.prompt("create it") == "created"

        result = ToolResult(
            "write-1",
            '{"bytes_written":9,"operation":"created","path":"note.txt"}\n',
        )
        assert provider.requests[1].history[-2:] == (call, result)
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "approved\n"
        assert len(approval_requests) == 1
        exact = approval_requests[0].identity
        assert exact.tool_use_id == "write-1"
        assert exact.arguments == call.arguments
        audit = session._writer.state.action_audits[-1]
        assert audit.status == ActionAuditStatus.SUCCEEDED
        assert audit.execution_outcome == ActionExecutionOutcome.SUCCEEDED
        assert audit.result_code == "created"
    finally:
        session.close()


@pytest.mark.parametrize(
    ("resolution", "expected_status", "message"),
    [
        (ApprovalResolution.REJECT, ActionAuditStatus.REJECTED, "action approval rejected"),
        (ApprovalResolution.CANCEL, ActionAuditStatus.CANCELLED, "action approval cancelled"),
    ],
)
def test_workspace_write_ask_reject_or_cancel_returns_tool_error_and_commits(
    tmp_path: Path,
    resolution: ApprovalResolution,
    expected_status: ActionAuditStatus,
    message: str,
) -> None:
    provider = ToolProvider([write_call(), AssistantText("stopped")])
    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=lambda _request: resolution,
    )
    try:
        assert session.prompt("write") == "stopped"

        result = provider.requests[1].history[-1]
        assert result == ToolResult("write-1", message, is_error=True)
        assert not (tmp_path / "note.txt").exists()
        assert session._writer.state.action_audits[-1].status == expected_status
    finally:
        session.close()


def test_workspace_write_auto_creates_then_overwrites_using_host_observed_state(
    tmp_path: Path,
) -> None:
    provider = ToolProvider(
        [
            write_call(content="first\n", tool_use_id="write-1"),
            AssistantText("first done"),
            write_call(content="second\n", tool_use_id="write-2"),
            AssistantText("second done"),
        ]
    )
    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
    )
    try:
        assert session.prompt("first") == "first done"
        assert session.prompt("second") == "second done"

        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "second\n"
        audits = session._writer.state.action_audits
        assert [audit.identity.action.value for audit in audits] == [
            "workspace-create",
            "workspace-overwrite",
        ]
        assert [audit.result_code for audit in audits] == ["created", "overwritten"]
    finally:
        session.close()


def test_accepted_approval_becomes_stale_if_target_changes_while_waiting(tmp_path: Path) -> None:
    provider = ToolProvider([write_call(), AssistantText("must not be reached")])

    def mutate_then_accept(_request: HumanApprovalRequest) -> ApprovalResolution:
        (tmp_path / "note.txt").write_text("external\n", encoding="utf-8")
        return ApprovalResolution.ACCEPT

    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=mutate_then_accept,
    )
    try:
        with pytest.raises(ApprovalGrantError) as caught:
            session.prompt("write")

        assert caught.value.code == ApprovalGrantRejection.STALE_PRECONDITION
        assert len(provider.requests) == 1
        assert session.history == ()
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "external\n"
        assert session._writer.state.action_audits[-1].status == ActionAuditStatus.ABANDONED
    finally:
        session.close()


def test_provider_continuation_failure_after_write_preserves_effect_and_audit_without_turn_commit(
    tmp_path: Path,
) -> None:
    provider = ToolProvider([write_call(), RuntimeError("provider continuation failed")])
    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
    )
    try:
        with pytest.raises(RuntimeError, match="provider continuation failed"):
            session.prompt("write")

        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello\n"
        assert session.history == ()
        audit = session._writer.state.action_audits[-1]
        assert audit.status == ActionAuditStatus.SUCCEEDED
        assert session._writer.state.records[-1].record_type == "turn_failed"
    finally:
        session.close()


def test_turn_commit_failure_after_write_preserves_truthful_effect_and_action_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = ToolProvider([write_call(), AssistantText("done")])
    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
    )

    def fail_commit(*_args, **_kwargs) -> None:
        raise SessionStoreError("injected turn commit failure")

    monkeypatch.setattr(session._writer, "append_turn", fail_commit)
    try:
        with pytest.raises(SessionStoreError, match="injected turn commit failure"):
            session.prompt("write")

        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello\n"
        assert session.history == ()
        assert session._writer.state.action_audits[-1].status == ActionAuditStatus.SUCCEEDED
        assert session._writer.state.records[-1].record_type == "turn_failed"
    finally:
        session.close()


def edit_call(
    *,
    old_text: str = "before",
    new_text: str = "after",
    tool_use_id: str = "edit-1",
) -> ToolUse:
    return ToolUse(
        tool_use_id,
        "edit_file",
        ToolArguments.from_mapping(
            {"path": "note.txt", "old_text": old_text, "new_text": new_text}
        ),
    )


def test_model_visible_edit_ask_accept_edits_and_commits_exact_causality(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("before\n", encoding="utf-8")
    call = edit_call()
    provider = ToolProvider([call, AssistantText("edited")])
    approval_requests: list[HumanApprovalRequest] = []

    def approve(request: HumanApprovalRequest) -> ApprovalResolution:
        approval_requests.append(request)
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "before\n"
        return ApprovalResolution.ACCEPT

    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=approve,
    )
    try:
        assert session.prompt("change before to after") == "edited"

        result = ToolResult(
            "edit-1",
            '{"bytes_written":6,"operation":"edited","path":"note.txt","replacements":1}\n',
        )
        assert provider.requests[1].history[-2:] == (call, result)
        assert session.history == (
            UserMessage("change before to after"),
            call,
            result,
            AssistantText("edited"),
        )
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"
        assert len(approval_requests) == 1
        identity = approval_requests[0].identity
        assert identity.tool_name == "edit_file"
        assert identity.arguments == call.arguments
        assert identity.action.value == "workspace-overwrite"
        audit = session.action_audits()[-1]
        assert audit.status == ActionAuditStatus.SUCCEEDED
        assert audit.execution_outcome == ActionExecutionOutcome.SUCCEEDED
        assert audit.result_code == "edited"
    finally:
        session.close()


def test_model_visible_edit_read_only_denial_is_audited_and_keeps_source(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("before\n", encoding="utf-8")
    call = edit_call()
    provider = ToolProvider([call, AssistantText("not edited")])
    session = open_session(tmp_path, provider)
    try:
        assert session.prompt("edit it") == "not edited"
        denied = ToolResult("edit-1", "permission denied: denied_read_only_mode", is_error=True)
        assert provider.requests[1].history[-2:] == (call, denied)
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "before\n"
        assert session.action_audits()[-1].status == ActionAuditStatus.DENIED
    finally:
        session.close()


def test_model_visible_edit_hard_match_rejection_has_no_action_audit(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("before before\n", encoding="utf-8")
    call = edit_call()
    provider = ToolProvider([call, AssistantText("ambiguous")])
    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.AUTO,
    )
    try:
        assert session.prompt("edit one occurrence") == "ambiguous"
        rejected = ToolResult("edit-1", "edit_file old_text matches more than once", is_error=True)
        assert provider.requests[1].history[-2:] == (call, rejected)
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "before before\n"
        assert session.action_audits() == ()
    finally:
        session.close()


def test_model_visible_edit_accepted_approval_rejects_stale_source(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("before\n", encoding="utf-8")
    provider = ToolProvider([edit_call(), AssistantText("must not be reached")])

    def mutate_then_accept(_request: HumanApprovalRequest) -> ApprovalResolution:
        (tmp_path / "note.txt").write_text("external\n", encoding="utf-8")
        return ApprovalResolution.ACCEPT

    session = open_session(
        tmp_path,
        provider,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        approval_mode=ApprovalMode.ASK,
        approval_handler=mutate_then_accept,
    )
    try:
        with pytest.raises(ApprovalGrantError) as caught:
            session.prompt("edit it")

        assert caught.value.code == ApprovalGrantRejection.STALE_PRECONDITION
        assert len(provider.requests) == 1
        assert session.history == ()
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "external\n"
        assert session.action_audits()[-1].status == ActionAuditStatus.ABANDONED
    finally:
        session.close()
