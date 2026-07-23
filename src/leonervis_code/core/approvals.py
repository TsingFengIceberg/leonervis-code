"""Single-use approval grants bound to exact Host action identities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from leonervis_code.core.actions import ActionIdentity, canonical_uuid4
from leonervis_code.core.permissions import (
    PermissionDecision,
    PermissionGate,
    PermissionReason,
    PermissionRequest,
    PermissionResult,
)


class ApprovalGrantRejection(StrEnum):
    """Stable reasons why an approval grant cannot authorize execution."""

    ACTION_IDENTITY_MISMATCH = "action_identity_mismatch"
    STALE_LEASE = "stale_lease"
    STALE_PRECONDITION = "stale_precondition"
    ALREADY_CONSUMED = "already_consumed"


class ApprovalGrantError(RuntimeError):
    """Reject stale, mismatched, or replayed approval consumption."""

    def __init__(self, code: ApprovalGrantRejection) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class ApprovalGrantConsumption:
    """One proof that an exact grant was consumed exactly once in memory."""

    grant_id: str
    action_request_id: str
    action_digest: str
    permission_reason: PermissionReason


class ApprovalGrant:
    """One local non-bearer grant whose exact action may be consumed once."""

    def __init__(
        self,
        *,
        grant_id: str,
        action_identity: ActionIdentity,
        permission_request: PermissionRequest,
        permission_result: PermissionResult,
    ) -> None:
        canonical_uuid4(grant_id, "approval grant ID")
        if type(action_identity) is not ActionIdentity:
            raise ValueError("approval action identity is invalid")
        if type(permission_request) is not PermissionRequest:
            raise ValueError("approval permission request is invalid")
        if type(permission_result) is not PermissionResult:
            raise ValueError("approval permission result is invalid")
        if action_identity.action != permission_request.action:
            raise ValueError("approval action does not match permission request")
        expected = PermissionGate().evaluate(permission_request)
        if permission_result != expected:
            raise ValueError("approval permission result does not match policy")
        if permission_result.decision != PermissionDecision.ASK:
            raise ValueError("approval grant requires an ask decision")

        self._grant_id = grant_id
        self._action_identity = action_identity
        self._permission_reason = permission_result.reason
        self._consumed = False
        self._lock = Lock()

    @classmethod
    def issue(
        cls,
        *,
        grant_id: str,
        action_identity: ActionIdentity,
        permission_request: PermissionRequest,
        permission_result: PermissionResult,
    ) -> ApprovalGrant:
        """Issue only for the exact deterministic policy result ``ask``."""
        return cls(
            grant_id=grant_id,
            action_identity=action_identity,
            permission_request=permission_request,
            permission_result=permission_result,
        )

    @property
    def grant_id(self) -> str:
        return self._grant_id

    @property
    def action_identity(self) -> ActionIdentity:
        return self._action_identity

    @property
    def permission_reason(self) -> PermissionReason:
        return self._permission_reason

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def consume(self, current_identity: ActionIdentity) -> ApprovalGrantConsumption:
        """Consume once only when action, lease, and precondition remain exact."""
        if type(current_identity) is not ActionIdentity:
            raise ValueError("current action identity is invalid")
        with self._lock:
            if self._consumed:
                raise ApprovalGrantError(ApprovalGrantRejection.ALREADY_CONSUMED)
            approved = self._action_identity
            if (
                current_identity.request_id != approved.request_id
                or current_identity.tool_use_id != approved.tool_use_id
                or current_identity.tool_name != approved.tool_name
                or current_identity.arguments != approved.arguments
                or current_identity.action != approved.action
                or current_identity.workspace_fingerprint != approved.workspace_fingerprint
                or current_identity.version != approved.version
            ):
                raise ApprovalGrantError(ApprovalGrantRejection.ACTION_IDENTITY_MISMATCH)
            if current_identity.lease != approved.lease:
                raise ApprovalGrantError(ApprovalGrantRejection.STALE_LEASE)
            if current_identity.precondition != approved.precondition:
                raise ApprovalGrantError(ApprovalGrantRejection.STALE_PRECONDITION)
            if current_identity.digest != approved.digest:
                raise ApprovalGrantError(ApprovalGrantRejection.ACTION_IDENTITY_MISMATCH)
            self._consumed = True
            return ApprovalGrantConsumption(
                grant_id=self._grant_id,
                action_request_id=approved.request_id,
                action_digest=approved.digest,
                permission_reason=self._permission_reason,
            )
