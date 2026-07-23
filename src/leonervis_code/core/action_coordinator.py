"""Host orchestration for permission, approval, durable audit, and execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from leonervis_code.core.actions import ActionIdentity
from leonervis_code.core.approvals import ApprovalGrant
from leonervis_code.core.contracts import ToolResult
from leonervis_code.core.permissions import (
    ApprovalMode,
    PermissionDecision,
    PermissionGate,
    PermissionMode,
    PermissionRequest,
    PermissionResult,
)
from leonervis_code.session_records import (
    ActionAuthorization,
    ActionExecutionOutcome,
    ApprovalAuditOutcome,
    BindingSnapshot,
)
from leonervis_code.session_store import SessionWriter


class ApprovalResolution(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    CANCEL = "cancel"


@dataclass(frozen=True)
class HumanApprovalRequest:
    identity: ActionIdentity
    permission_result: PermissionResult


@dataclass(frozen=True)
class ActionExecutionResult:
    tool_result: ToolResult
    outcome: ActionExecutionOutcome
    result_code: str
    audit_message: str

    def __post_init__(self) -> None:
        if type(self.tool_result) is not ToolResult:
            raise ValueError("action execution tool result is invalid")
        if type(self.outcome) is not ActionExecutionOutcome:
            raise ValueError("action execution outcome is invalid")
        if not isinstance(self.result_code, str) or not self.result_code:
            raise ValueError("action execution result code is invalid")
        if not isinstance(self.audit_message, str):
            raise ValueError("action execution audit message is invalid")
        if (self.outcome == ActionExecutionOutcome.SUCCEEDED) == self.tool_result.is_error:
            raise ValueError("action execution outcome does not match tool result")


@dataclass(frozen=True)
class ActionCoordinatorResult:
    tool_result: ToolResult
    permission_result: PermissionResult
    approval_resolution: ApprovalResolution | None
    executed: bool


class ActionIdentityChangedError(RuntimeError):
    """Raised when the prepared exact action is stale before durable execution start."""


ApprovalHandler = Callable[[HumanApprovalRequest], ApprovalResolution]
ActionRevalidator = Callable[[ActionIdentity], ActionIdentity]
ActionExecutor = Callable[[ActionIdentity], ActionExecutionResult]
UuidFactory = Callable[[], UUID | str]


class ActionCoordinator:
    """Run one exact action through policy, optional human approval, and durable audit."""

    def __init__(
        self,
        *,
        writer: SessionWriter,
        approval_handler: ApprovalHandler,
        permission_gate: PermissionGate | None = None,
        uuid_factory: UuidFactory = uuid4,
    ) -> None:
        self._writer = writer
        self._approval_handler = approval_handler
        self._permission_gate = permission_gate or PermissionGate()
        self._uuid_factory = uuid_factory

    def run(
        self,
        *,
        identity: ActionIdentity,
        binding: BindingSnapshot,
        permission_mode: PermissionMode,
        approval_mode: ApprovalMode,
        revalidate: ActionRevalidator,
        execute: ActionExecutor,
    ) -> ActionCoordinatorResult:
        request = PermissionRequest(permission_mode, approval_mode, identity.action)
        self._writer.action_requested(
            identity=identity,
            binding=binding,
            permission_mode=permission_mode,
            approval_mode=approval_mode,
        )
        permission = self._permission_gate.evaluate(request)
        self._writer.permission_decided(identity=identity, result=permission)

        if permission.decision == PermissionDecision.DENY:
            return ActionCoordinatorResult(
                ToolResult(
                    identity.tool_use_id,
                    f"permission denied: {permission.reason.value}",
                    is_error=True,
                ),
                permission,
                None,
                False,
            )

        grant = None
        resolution = None
        authorization = ActionAuthorization.POLICY_ALLOW
        grant_id = None
        if permission.decision == PermissionDecision.ASK:
            resolution = self._approval_handler(HumanApprovalRequest(identity, permission))
            if type(resolution) is not ApprovalResolution:
                raise ValueError("approval handler returned an invalid resolution")
            if resolution == ApprovalResolution.REJECT:
                self._writer.approval_resolved(
                    identity=identity,
                    outcome=ApprovalAuditOutcome.REJECTED,
                    grant_id=None,
                )
                return ActionCoordinatorResult(
                    ToolResult(identity.tool_use_id, "action approval rejected", is_error=True),
                    permission,
                    resolution,
                    False,
                )
            if resolution == ApprovalResolution.CANCEL:
                self._writer.approval_resolved(
                    identity=identity,
                    outcome=ApprovalAuditOutcome.CANCELLED,
                    grant_id=None,
                )
                return ActionCoordinatorResult(
                    ToolResult(identity.tool_use_id, "action approval cancelled", is_error=True),
                    permission,
                    resolution,
                    False,
                )
            grant_id = _uuid4_text(self._uuid_factory(), "approval grant ID")
            grant = ApprovalGrant.issue(
                grant_id=grant_id,
                action_identity=identity,
                permission_request=request,
                permission_result=permission,
            )
            self._writer.approval_resolved(
                identity=identity,
                outcome=ApprovalAuditOutcome.ACCEPTED,
                grant_id=grant_id,
            )
            authorization = ActionAuthorization.APPROVAL_GRANT

        current = revalidate(identity)
        if type(current) is not ActionIdentity:
            raise ValueError("action revalidator returned an invalid identity")
        if grant is not None:
            grant.consume(current)
        elif current != identity or current.digest != identity.digest:
            raise ActionIdentityChangedError("action identity changed before execution")

        self._writer.action_execution_started(
            identity=identity,
            authorization=authorization,
            grant_id=grant_id,
        )
        try:
            execution = execute(identity)
        except Exception:
            execution = ActionExecutionResult(
                ToolResult(identity.tool_use_id, "tool executor failed", is_error=True),
                ActionExecutionOutcome.FAILED,
                "executor_error",
                "tool executor raised an exception",
            )
        if type(execution) is not ActionExecutionResult:
            raise ValueError("action executor returned an invalid result")
        if execution.tool_result.tool_use_id != identity.tool_use_id:
            raise ValueError("action executor result does not match tool_use ID")
        self._writer.action_execution_finished(
            identity=identity,
            outcome=execution.outcome,
            result_code=execution.result_code,
            message=execution.audit_message,
        )
        return ActionCoordinatorResult(
            execution.tool_result,
            permission,
            resolution,
            True,
        )


def _uuid4_text(value: UUID | str, label: str) -> str:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{label} must be a canonical UUID4") from None
    if parsed.version != 4 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical UUID4")
    return str(parsed)
