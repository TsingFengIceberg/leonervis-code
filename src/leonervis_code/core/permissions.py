"""Pure permission policy contracts for actions requested through the Host."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PermissionMode(StrEnum):
    """The configured capability ceiling for Host actions."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalMode(StrEnum):
    """Whether an in-scope controlled action requires human confirmation."""

    ASK = "ask"
    AUTO = "auto"


class PermissionAction(StrEnum):
    """One trusted Host classification used by the pure policy matrix."""

    WORKSPACE_READ = "workspace-read"
    WORKSPACE_CREATE = "workspace-create"
    WORKSPACE_OVERWRITE = "workspace-overwrite"
    DANGEROUS = "dangerous"
    UNKNOWN = "unknown"


class PermissionDecision(StrEnum):
    """The only policy outcomes understood by later Host orchestration."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionReason(StrEnum):
    """Stable machine-readable attribution for one permission decision."""

    ALLOWED_WORKSPACE_READ = "allowed_workspace_read"
    ALLOWED_WORKSPACE_CREATE_AUTO = "allowed_workspace_create_auto"
    ALLOWED_WORKSPACE_OVERWRITE_AUTO = "allowed_workspace_overwrite_auto"
    ALLOWED_DANGEROUS_AUTO = "allowed_dangerous_auto"
    APPROVAL_REQUIRED_WORKSPACE_CREATE = "approval_required_workspace_create"
    APPROVAL_REQUIRED_WORKSPACE_OVERWRITE = "approval_required_workspace_overwrite"
    APPROVAL_REQUIRED_DANGEROUS = "approval_required_dangerous"
    DENIED_READ_ONLY_MODE = "denied_read_only_mode"
    DENIED_WORKSPACE_WRITE_MODE = "denied_workspace_write_mode"
    DENIED_UNKNOWN_ACTION = "denied_unknown_action"


_REASON_DECISIONS = {
    PermissionReason.ALLOWED_WORKSPACE_READ: PermissionDecision.ALLOW,
    PermissionReason.ALLOWED_WORKSPACE_CREATE_AUTO: PermissionDecision.ALLOW,
    PermissionReason.ALLOWED_WORKSPACE_OVERWRITE_AUTO: PermissionDecision.ALLOW,
    PermissionReason.ALLOWED_DANGEROUS_AUTO: PermissionDecision.ALLOW,
    PermissionReason.APPROVAL_REQUIRED_WORKSPACE_CREATE: PermissionDecision.ASK,
    PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE: PermissionDecision.ASK,
    PermissionReason.APPROVAL_REQUIRED_DANGEROUS: PermissionDecision.ASK,
    PermissionReason.DENIED_READ_ONLY_MODE: PermissionDecision.DENY,
    PermissionReason.DENIED_WORKSPACE_WRITE_MODE: PermissionDecision.DENY,
    PermissionReason.DENIED_UNKNOWN_ACTION: PermissionDecision.DENY,
}


@dataclass(frozen=True)
class PermissionRequest:
    """One immutable, already-classified input to the pure permission matrix."""

    permission_mode: PermissionMode
    approval_mode: ApprovalMode
    action: PermissionAction

    def __post_init__(self) -> None:
        if type(self.permission_mode) is not PermissionMode:
            raise ValueError("permission mode is invalid")
        if type(self.approval_mode) is not ApprovalMode:
            raise ValueError("approval mode is invalid")
        if type(self.action) is not PermissionAction:
            raise ValueError("permission action is invalid")


@dataclass(frozen=True)
class PermissionResult:
    """One immutable decision with stable machine-readable attribution."""

    decision: PermissionDecision
    reason: PermissionReason

    def __post_init__(self) -> None:
        if type(self.decision) is not PermissionDecision:
            raise ValueError("permission decision is invalid")
        if type(self.reason) is not PermissionReason:
            raise ValueError("permission reason is invalid")
        if _REASON_DECISIONS[self.reason] != self.decision:
            raise ValueError("permission reason does not match decision")


class PermissionGate:
    """Evaluate capability and approval policy without I/O or execution."""

    def evaluate(self, request: PermissionRequest) -> PermissionResult:
        """Return the deterministic allow, ask, or deny result for ``request``."""
        if type(request) is not PermissionRequest:
            raise ValueError("permission request is invalid")

        if request.action == PermissionAction.WORKSPACE_READ:
            return PermissionResult(
                PermissionDecision.ALLOW,
                PermissionReason.ALLOWED_WORKSPACE_READ,
            )
        if request.action == PermissionAction.UNKNOWN:
            return PermissionResult(
                PermissionDecision.DENY,
                PermissionReason.DENIED_UNKNOWN_ACTION,
            )
        if request.permission_mode == PermissionMode.READ_ONLY:
            return PermissionResult(
                PermissionDecision.DENY,
                PermissionReason.DENIED_READ_ONLY_MODE,
            )

        if request.action == PermissionAction.DANGEROUS:
            if request.permission_mode != PermissionMode.DANGER_FULL_ACCESS:
                return PermissionResult(
                    PermissionDecision.DENY,
                    PermissionReason.DENIED_WORKSPACE_WRITE_MODE,
                )
            if request.approval_mode == ApprovalMode.ASK:
                return PermissionResult(
                    PermissionDecision.ASK,
                    PermissionReason.APPROVAL_REQUIRED_DANGEROUS,
                )
            return PermissionResult(
                PermissionDecision.ALLOW,
                PermissionReason.ALLOWED_DANGEROUS_AUTO,
            )

        if request.action == PermissionAction.WORKSPACE_CREATE:
            if request.approval_mode == ApprovalMode.ASK:
                return PermissionResult(
                    PermissionDecision.ASK,
                    PermissionReason.APPROVAL_REQUIRED_WORKSPACE_CREATE,
                )
            return PermissionResult(
                PermissionDecision.ALLOW,
                PermissionReason.ALLOWED_WORKSPACE_CREATE_AUTO,
            )

        if request.action == PermissionAction.WORKSPACE_OVERWRITE:
            if request.approval_mode == ApprovalMode.ASK:
                return PermissionResult(
                    PermissionDecision.ASK,
                    PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE,
                )
            return PermissionResult(
                PermissionDecision.ALLOW,
                PermissionReason.ALLOWED_WORKSPACE_OVERWRITE_AUTO,
            )

        return PermissionResult(
            PermissionDecision.DENY,
            PermissionReason.DENIED_UNKNOWN_ACTION,
        )
