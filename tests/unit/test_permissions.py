from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from leonervis_code.core.permissions import (
    ApprovalMode,
    PermissionAction,
    PermissionDecision,
    PermissionGate,
    PermissionMode,
    PermissionReason,
    PermissionRequest,
    PermissionResult,
)


def request(
    action: PermissionAction,
    permission_mode: PermissionMode,
    approval_mode: ApprovalMode,
) -> PermissionRequest:
    return PermissionRequest(permission_mode, approval_mode, action)


def test_permission_contract_values_are_stable() -> None:
    assert [item.value for item in PermissionMode] == [
        "read-only",
        "workspace-write",
        "danger-full-access",
    ]
    assert [item.value for item in ApprovalMode] == ["ask", "auto"]
    assert [item.value for item in PermissionAction] == [
        "workspace-read",
        "workspace-create",
        "workspace-overwrite",
        "dangerous",
        "unknown",
    ]
    assert [item.value for item in PermissionDecision] == ["allow", "ask", "deny"]
    assert [item.value for item in PermissionReason] == [
        "allowed_workspace_read",
        "allowed_workspace_create_auto",
        "allowed_workspace_overwrite_auto",
        "allowed_dangerous_auto",
        "approval_required_workspace_create",
        "approval_required_workspace_overwrite",
        "approval_required_dangerous",
        "denied_read_only_mode",
        "denied_workspace_write_mode",
        "denied_unknown_action",
    ]


@pytest.mark.parametrize("permission_mode", list(PermissionMode))
@pytest.mark.parametrize("approval_mode", list(ApprovalMode))
def test_workspace_reads_are_always_allowed(permission_mode, approval_mode) -> None:
    result = PermissionGate().evaluate(
        request(PermissionAction.WORKSPACE_READ, permission_mode, approval_mode)
    )

    assert result == PermissionResult(
        PermissionDecision.ALLOW,
        PermissionReason.ALLOWED_WORKSPACE_READ,
    )


@pytest.mark.parametrize(
    ("action", "ask_reason", "auto_reason"),
    [
        (
            PermissionAction.WORKSPACE_CREATE,
            PermissionReason.APPROVAL_REQUIRED_WORKSPACE_CREATE,
            PermissionReason.ALLOWED_WORKSPACE_CREATE_AUTO,
        ),
        (
            PermissionAction.WORKSPACE_OVERWRITE,
            PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE,
            PermissionReason.ALLOWED_WORKSPACE_OVERWRITE_AUTO,
        ),
    ],
)
def test_workspace_writes_follow_mode_and_approval(action, ask_reason, auto_reason) -> None:
    gate = PermissionGate()

    for approval_mode in ApprovalMode:
        assert gate.evaluate(request(action, PermissionMode.READ_ONLY, approval_mode)) == (
            PermissionResult(
                PermissionDecision.DENY,
                PermissionReason.DENIED_READ_ONLY_MODE,
            )
        )
    for permission_mode in (PermissionMode.WORKSPACE_WRITE, PermissionMode.DANGER_FULL_ACCESS):
        assert gate.evaluate(request(action, permission_mode, ApprovalMode.ASK)) == (
            PermissionResult(PermissionDecision.ASK, ask_reason)
        )
        assert gate.evaluate(request(action, permission_mode, ApprovalMode.AUTO)) == (
            PermissionResult(PermissionDecision.ALLOW, auto_reason)
        )


def test_dangerous_actions_require_danger_full_access() -> None:
    gate = PermissionGate()

    for approval_mode in ApprovalMode:
        assert gate.evaluate(
            request(PermissionAction.DANGEROUS, PermissionMode.READ_ONLY, approval_mode)
        ) == PermissionResult(
            PermissionDecision.DENY,
            PermissionReason.DENIED_READ_ONLY_MODE,
        )
        assert gate.evaluate(
            request(PermissionAction.DANGEROUS, PermissionMode.WORKSPACE_WRITE, approval_mode)
        ) == PermissionResult(
            PermissionDecision.DENY,
            PermissionReason.DENIED_WORKSPACE_WRITE_MODE,
        )
    assert gate.evaluate(
        request(PermissionAction.DANGEROUS, PermissionMode.DANGER_FULL_ACCESS, ApprovalMode.ASK)
    ) == PermissionResult(
        PermissionDecision.ASK,
        PermissionReason.APPROVAL_REQUIRED_DANGEROUS,
    )
    assert gate.evaluate(
        request(PermissionAction.DANGEROUS, PermissionMode.DANGER_FULL_ACCESS, ApprovalMode.AUTO)
    ) == PermissionResult(
        PermissionDecision.ALLOW,
        PermissionReason.ALLOWED_DANGEROUS_AUTO,
    )


@pytest.mark.parametrize("permission_mode", list(PermissionMode))
@pytest.mark.parametrize("approval_mode", list(ApprovalMode))
def test_unknown_actions_fail_closed(permission_mode, approval_mode) -> None:
    result = PermissionGate().evaluate(
        request(PermissionAction.UNKNOWN, permission_mode, approval_mode)
    )

    assert result == PermissionResult(
        PermissionDecision.DENY,
        PermissionReason.DENIED_UNKNOWN_ACTION,
    )


def test_permission_request_and_result_are_frozen() -> None:
    permission_request = request(
        PermissionAction.WORKSPACE_READ,
        PermissionMode.READ_ONLY,
        ApprovalMode.ASK,
    )
    result = PermissionGate().evaluate(permission_request)

    with pytest.raises(FrozenInstanceError):
        permission_request.action = PermissionAction.UNKNOWN
    with pytest.raises(FrozenInstanceError):
        result.decision = PermissionDecision.DENY


@pytest.mark.parametrize(
    "permission_request",
    [
        lambda: PermissionRequest("read-only", ApprovalMode.ASK, PermissionAction.WORKSPACE_READ),
        lambda: PermissionRequest(PermissionMode.READ_ONLY, "ask", PermissionAction.WORKSPACE_READ),
        lambda: PermissionRequest(PermissionMode.READ_ONLY, ApprovalMode.ASK, "workspace-read"),
    ],
)
def test_permission_request_rejects_untyped_values(permission_request) -> None:
    with pytest.raises(ValueError, match="invalid"):
        permission_request()


def test_permission_result_rejects_untyped_or_mismatched_values() -> None:
    with pytest.raises(ValueError, match="decision"):
        PermissionResult("allow", PermissionReason.ALLOWED_WORKSPACE_READ)
    with pytest.raises(ValueError, match="reason"):
        PermissionResult(PermissionDecision.ALLOW, "allowed_workspace_read")
    with pytest.raises(ValueError, match="does not match"):
        PermissionResult(PermissionDecision.DENY, PermissionReason.ALLOWED_WORKSPACE_READ)
    with pytest.raises(ValueError, match="request"):
        PermissionGate().evaluate("not a request")
