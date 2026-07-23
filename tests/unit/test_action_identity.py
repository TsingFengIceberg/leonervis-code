from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from threading import Barrier, Thread

import pytest

from leonervis_code.core.actions import (
    ACTION_IDENTITY_VERSION,
    ActionIdentity,
    ActionLease,
    ActionPrecondition,
    ActionPreconditionKind,
)
from leonervis_code.core.approvals import (
    ApprovalGrant,
    ApprovalGrantError,
    ApprovalGrantRejection,
)
from leonervis_code.core.contracts import ToolArguments
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

SESSION_ID = "12345678-1234-4234-9234-123456789abc"
LEASE_ID = "22345678-1234-4234-9234-123456789abc"
REQUEST_ID = "32345678-1234-4234-9234-123456789abc"
GRANT_ID = "42345678-1234-4234-9234-123456789abc"
CONTEXT_ID = f"ctx-v1-{'1' * 64}"
WORKSPACE_FINGERPRINT = f"v1-{'2' * 64}"
STATE_FINGERPRINT = "3" * 64


def identity(**changes) -> ActionIdentity:
    values = {
        "request_id": REQUEST_ID,
        "tool_use_id": "tool-1",
        "tool_name": "write_file",
        "arguments": ToolArguments.from_mapping({"content": "hello\n", "path": "notes.txt"}),
        "action": PermissionAction.WORKSPACE_CREATE,
        "workspace_fingerprint": WORKSPACE_FINGERPRINT,
        "lease": ActionLease(
            session_id=SESSION_ID,
            lease_id=LEASE_ID,
            runtime_generation=2,
            context_id=CONTEXT_ID,
        ),
        "precondition": ActionPrecondition.path_absent(),
    }
    values.update(changes)
    return ActionIdentity(**values)


def ask_request(action: PermissionAction = PermissionAction.WORKSPACE_CREATE) -> PermissionRequest:
    return PermissionRequest(PermissionMode.WORKSPACE_WRITE, ApprovalMode.ASK, action)


def grant(action_identity: ActionIdentity | None = None) -> ApprovalGrant:
    exact = action_identity or identity()
    request = ask_request(exact.action)
    return ApprovalGrant.issue(
        grant_id=GRANT_ID,
        action_identity=exact,
        permission_request=request,
        permission_result=PermissionGate().evaluate(request),
    )


def test_action_identity_is_canonical_round_trippable_and_has_stable_digest() -> None:
    first = identity()
    reordered = identity(
        arguments=ToolArguments.from_mapping({"path": "notes.txt", "content": "hello\n"})
    )

    assert first == reordered
    assert first.version == ACTION_IDENTITY_VERSION
    assert first.canonical_json == reordered.canonical_json
    assert ActionIdentity.from_mapping(first.as_mapping()) == first
    assert first.digest == (
        "act-v1-65861399d86d6c67fb4dee2860053ebabe469cf7a1325f1daaf65f3b681984b0"
    )


@pytest.mark.parametrize(
    "changed",
    [
        identity(request_id="52345678-1234-4234-9234-123456789abc"),
        identity(tool_use_id="tool-2"),
        identity(tool_name="other_tool"),
        identity(arguments=ToolArguments.from_mapping({"path": "other.txt"})),
        identity(action=PermissionAction.WORKSPACE_OVERWRITE),
        identity(workspace_fingerprint=f"v1-{'4' * 64}"),
        identity(
            lease=ActionLease(
                SESSION_ID,
                "62345678-1234-4234-9234-123456789abc",
                2,
                CONTEXT_ID,
            )
        ),
        identity(precondition=ActionPrecondition.expected_state(STATE_FINGERPRINT)),
    ],
)
def test_every_exact_action_component_changes_the_digest(changed: ActionIdentity) -> None:
    assert changed.digest != identity().digest


def test_action_contracts_are_frozen_and_reject_untyped_or_malformed_values() -> None:
    exact = identity()
    with pytest.raises(FrozenInstanceError):
        exact.tool_name = "changed"
    with pytest.raises(FrozenInstanceError):
        exact.lease.runtime_generation = 3
    with pytest.raises(ValueError, match="precondition kind"):
        ActionPrecondition("none")
    with pytest.raises(ValueError, match="requires"):
        ActionPrecondition(ActionPreconditionKind.EXPECTED_STATE_SHA256)
    with pytest.raises(ValueError, match="must be null"):
        ActionPrecondition(ActionPreconditionKind.NONE, STATE_FINGERPRINT)
    with pytest.raises(ValueError, match="runtime generation"):
        ActionLease(SESSION_ID, LEASE_ID, True, CONTEXT_ID)
    with pytest.raises(ValueError, match="context ID"):
        ActionLease(SESSION_ID, LEASE_ID, 0, "ctx-v1-invalid")
    with pytest.raises(ValueError, match="context ID"):
        ActionLease(SESSION_ID, LEASE_ID, 0, f"ctx-v3-{'1' * 64}")
    with pytest.raises(ValueError, match="request ID"):
        identity(request_id="not-a-uuid")
    with pytest.raises(ValueError, match="permission action"):
        identity(action="workspace-create")
    with pytest.raises(ValueError, match="identity version"):
        identity(version=True)


def test_action_decoder_rejects_unknown_fields_and_invalid_nested_values() -> None:
    value = identity().as_mapping()
    value["extra"] = True
    with pytest.raises(ValueError, match="fields"):
        ActionIdentity.from_mapping(value)

    value = identity().as_mapping()
    value["lease"] = {**value["lease"], "runtime_generation": -1}
    with pytest.raises(ValueError, match="runtime generation"):
        ActionIdentity.from_mapping(value)

    value = identity().as_mapping()
    value["precondition"] = {"kind": "invented", "fingerprint": None}
    with pytest.raises(ValueError, match="precondition kind"):
        ActionIdentity.from_mapping(value)


def test_approval_grant_can_only_be_issued_for_the_exact_ask_policy_result() -> None:
    exact = identity()
    request = ask_request()
    result = PermissionGate().evaluate(request)
    issued = ApprovalGrant.issue(
        grant_id=GRANT_ID,
        action_identity=exact,
        permission_request=request,
        permission_result=result,
    )

    assert issued.grant_id == GRANT_ID
    assert issued.action_identity == exact
    assert issued.permission_reason == PermissionReason.APPROVAL_REQUIRED_WORKSPACE_CREATE
    assert issued.is_consumed is False

    with pytest.raises(ValueError, match="does not match permission request"):
        ApprovalGrant.issue(
            grant_id=GRANT_ID,
            action_identity=exact,
            permission_request=ask_request(PermissionAction.WORKSPACE_OVERWRITE),
            permission_result=PermissionResult(
                PermissionDecision.ASK,
                PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE,
            ),
        )
    with pytest.raises(ValueError, match="does not match policy"):
        ApprovalGrant.issue(
            grant_id=GRANT_ID,
            action_identity=exact,
            permission_request=request,
            permission_result=PermissionResult(
                PermissionDecision.ASK,
                PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE,
            ),
        )
    auto = PermissionRequest(
        PermissionMode.WORKSPACE_WRITE,
        ApprovalMode.AUTO,
        PermissionAction.WORKSPACE_CREATE,
    )
    with pytest.raises(ValueError, match="requires an ask"):
        ApprovalGrant.issue(
            grant_id=GRANT_ID,
            action_identity=exact,
            permission_request=auto,
            permission_result=PermissionGate().evaluate(auto),
        )


def test_approval_grant_consumes_once_for_the_exact_identity() -> None:
    issued = grant()

    consumption = issued.consume(identity())

    assert consumption.grant_id == GRANT_ID
    assert consumption.action_request_id == REQUEST_ID
    assert consumption.action_digest == identity().digest
    assert consumption.permission_reason == PermissionReason.APPROVAL_REQUIRED_WORKSPACE_CREATE
    assert issued.is_consumed is True
    with pytest.raises(ApprovalGrantError) as replay:
        issued.consume(identity())
    assert replay.value.code == ApprovalGrantRejection.ALREADY_CONSUMED


def test_approval_grant_rejects_action_lease_and_precondition_changes_without_consuming() -> None:
    issued = grant()
    with pytest.raises(ApprovalGrantError) as mismatch:
        issued.consume(identity(arguments=ToolArguments.from_mapping({"path": "other.txt"})))
    assert mismatch.value.code == ApprovalGrantRejection.ACTION_IDENTITY_MISMATCH
    assert issued.is_consumed is False

    stale_lease = replace(identity().lease, runtime_generation=3)
    with pytest.raises(ApprovalGrantError) as stale:
        issued.consume(identity(lease=stale_lease))
    assert stale.value.code == ApprovalGrantRejection.STALE_LEASE
    assert issued.is_consumed is False

    with pytest.raises(ApprovalGrantError) as changed_state:
        issued.consume(identity(precondition=ActionPrecondition.expected_state(STATE_FINGERPRINT)))
    assert changed_state.value.code == ApprovalGrantRejection.STALE_PRECONDITION
    assert issued.is_consumed is False


def test_concurrent_approval_consumption_has_exactly_one_winner() -> None:
    issued = grant()
    barrier = Barrier(3)
    successes = []
    failures = []

    def consume() -> None:
        barrier.wait()
        try:
            successes.append(issued.consume(identity()))
        except ApprovalGrantError as error:
            failures.append(error.code)

    threads = [Thread(target=consume), Thread(target=consume)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert failures == [ApprovalGrantRejection.ALREADY_CONSUMED]
