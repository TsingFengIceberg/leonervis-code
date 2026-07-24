from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.cli.presentation import (
    BLUE,
    GREEN,
    RED,
    RESET,
    YELLOW,
    render_context_inspection,
    render_action_audits,
    render_message,
    render_prompt,
    render_prompt_event,
    render_resume_rejection,
    render_runtime_status,
    render_runtime_switch,
    render_session_resume,
    render_switch_rejection,
)
from leonervis_code.providers.manager import CurrentTargetContextAssessment, RuntimeStatus
from leonervis_code.providers.request_context import (
    ContextFitDecision,
    ContextFitReport,
    RequestTokenCount,
    RequestTokenCountMethod,
)
from leonervis_code.core.compaction import CompactionTrigger
from leonervis_code.core.contracts import AssistantText, ToolArguments, UserMessage
from leonervis_code.core.permissions import (
    PermissionAction,
    PermissionDecision,
    PermissionReason,
    PermissionResult,
)
from leonervis_code.session import (
    AutoCompactionCommitted,
    AutoCompactionNotApplied,
    AutoCompactionStarted,
    CompactContextResult,
    EffectiveContextInspection,
    ResumeEffect,
    SessionResumeResult,
)
from leonervis_code.session_records import (
    ActionAuditStatus,
    ApprovalAuditOutcome,
    BindingSnapshot,
)
from leonervis_code.session_store import LatestUpdateStatus, SessionInfo
from leonervis_code.tools.glob import GlobTool
from leonervis_code.tools.grep import GrepTool
from leonervis_code.tools.read_file import ReadFileTool


@dataclass
class Info:
    session_id: str = "12345678-1234-4234-9234-123456789abc"


def status(*, mode="fake", profile=None, provider="fake", model=None):
    return RuntimeStatus(
        mode=mode,
        profile=profile,
        selection_source="default",
        provider_id=provider,
        protocol=None,
        selected_model=model,
        wire_model=model,
        base_url=None,
        base_url_source=None,
        credential_required=False,
        credential_present=False,
    )


def test_prompt_uses_short_session_and_runtime_identity_only() -> None:
    assert render_prompt(status(), Info(), color=False) == "leonervis[12345678|fake]> "
    assert (
        render_prompt(
            status(mode="real", profile="work-openai", provider="openai", model="gpt-5"),
            Info(),
            color=False,
        )
        == "leonervis[12345678|work-openai]> "
    )
    assert (
        render_prompt(
            status(mode="real", provider="openai", model="openai/gpt-5"),
            Info(),
            color=False,
        )
        == "leonervis[12345678|direct:openai]> "
    )


def test_action_audits_are_recent_bounded_and_redacted() -> None:
    def audit(sequence: int, path: str, status, *, result_code=None):
        return SimpleNamespace(
            identity=SimpleNamespace(
                tool_name="write_file",
                action=PermissionAction.WORKSPACE_CREATE,
                arguments=ToolArguments.from_mapping(
                    {"content": f"secret-content-{sequence}", "path": path}
                ),
            ),
            permission_result=PermissionResult(
                PermissionDecision.ASK,
                PermissionReason.APPROVAL_REQUIRED_WORKSPACE_CREATE,
            ),
            approval_outcome=ApprovalAuditOutcome.ACCEPTED,
            status=status,
            result_code=result_code,
            requested_sequence=sequence,
        )

    rendered = render_action_audits(
        (
            audit(1, "first.txt", ActionAuditStatus.SUCCEEDED, result_code="created"),
            audit(
                6,
                "odd\nname.txt",
                ActionAuditStatus.PARTIAL,
                result_code="durability\nunknown",
            ),
        ),
        1,
    )

    assert "Showing 1 most recent of 2 action audits." in rendered
    assert "Action #6: write_file" in rendered
    assert "class: workspace-create" in rendered
    assert "path: 'odd\\nname.txt'" in rendered
    assert "permission: ask (approval_required_workspace_create)" in rendered
    assert "approval: accepted" in rendered
    assert "result: partial (durability\\nunknown)" in rendered
    assert "first.txt" not in rendered
    assert "secret-content" not in rendered
    assert render_action_audits((), 20) == "No action audits yet."


def test_action_audits_explain_nonexecuted_and_interrupted_lifecycles() -> None:
    def audit(
        sequence: int,
        status: ActionAuditStatus,
        *,
        decision: PermissionDecision | None,
        reason: PermissionReason | None,
        approval: ApprovalAuditOutcome | None = None,
    ):
        permission = (
            PermissionResult(decision, reason)
            if decision is not None and reason is not None
            else None
        )
        return SimpleNamespace(
            identity=SimpleNamespace(
                tool_name="write_file",
                action=PermissionAction.WORKSPACE_OVERWRITE,
                arguments=ToolArguments.from_mapping({"content": "secret", "path": "note.txt"}),
            ),
            permission_result=permission,
            approval_outcome=approval,
            status=status,
            result_code=None,
            requested_sequence=sequence,
        )

    rendered = render_action_audits(
        (
            audit(1, ActionAuditStatus.REQUESTED, decision=None, reason=None),
            audit(
                2,
                ActionAuditStatus.DENIED,
                decision=PermissionDecision.DENY,
                reason=PermissionReason.DENIED_READ_ONLY_MODE,
            ),
            audit(
                3,
                ActionAuditStatus.AUTHORIZED,
                decision=PermissionDecision.ALLOW,
                reason=PermissionReason.ALLOWED_WORKSPACE_OVERWRITE_AUTO,
            ),
            audit(
                4,
                ActionAuditStatus.AWAITING_APPROVAL,
                decision=PermissionDecision.ASK,
                reason=PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE,
            ),
            audit(
                5,
                ActionAuditStatus.ABANDONED,
                decision=PermissionDecision.ASK,
                reason=PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE,
            ),
            audit(
                6,
                ActionAuditStatus.OUTCOME_UNKNOWN,
                decision=PermissionDecision.ASK,
                reason=PermissionReason.APPROVAL_REQUIRED_WORKSPACE_OVERWRITE,
                approval=ApprovalAuditOutcome.ACCEPTED,
            ),
        ),
        20,
    )

    assert "Action #1" in rendered
    assert "permission: pending\n  approval: not reached\n  result: requested" in rendered
    assert (
        "permission: deny (denied_read_only_mode)\n  approval: not requested\n  result: denied"
    ) in rendered
    assert (
        "permission: allow (allowed_workspace_overwrite_auto)\n"
        "  approval: not required\n"
        "  result: authorized"
    ) in rendered
    assert "approval: pending\n  result: awaiting-approval" in rendered
    assert "approval: not recorded\n  result: abandoned" in rendered
    assert "approval: accepted\n  result: outcome-unknown" in rendered


def test_prompt_omits_model_and_sanitizes_runtime_fields() -> None:
    first = status(mode="real", profile="safe|name\x1b[31m", provider="custom", model="one")
    second = status(mode="real", profile="safe|name\x1b[31m", provider="custom", model="two")

    assert render_prompt(first, Info(), color=False) == "leonervis[12345678|safe?name??31m]> "
    assert render_prompt(first, Info(), color=False) == render_prompt(second, Info(), color=False)

    long = status(mode="real", profile="a" * 40, provider="custom")
    assert render_prompt(long, Info(), color=False) == (
        "leonervis[12345678|aaaaaaaaaaaaaaaaaaaaa...]> "
    )


def test_prompt_has_safe_fallbacks() -> None:
    assert render_prompt(None, None, color=False) == "leonervis> "
    assert render_prompt(status(), None, color=False) == "leonervis[fake]> "
    assert render_prompt(None, Info(), color=False) == "leonervis[12345678]> "
    assert render_prompt(status(), Info("bad"), color=False) == "leonervis[unknown|fake]> "


def test_runtime_status_renders_context_capability_without_changing_prompt() -> None:
    resolved = RuntimeStatus(
        **{
            **status(
                mode="real", profile="work", provider="anthropic", model="claude-opus-4-8"
            ).__dict__,
            "protocol": "anthropic_messages",
            "base_url": "https://api.anthropic.com",
            "base_url_source": "default",
            "context_window_tokens": 1_000_000,
            "context_window_source": "builtin_catalog",
        }
    )

    rendered = render_runtime_status(resolved)

    assert "Context window: 1000000 tokens (builtin_catalog)" in rendered
    assert "1000000" not in render_prompt(resolved, Info(), color=False)


def inspection(tmp_path, report=None, diagnostic=None, *history):
    loop = AgentLoop(
        None,
        ReadFileTool(tmp_path),
        GlobTool(tmp_path),
        GrepTool(tmp_path),
        initial_history=tuple(history),
    )
    target = CurrentTargetContextAssessment(status(), report, diagnostic)
    return EffectiveContextInspection(loop.effective_context_snapshot(), target)


def test_context_inspection_renders_fit_unknown_and_capacity(tmp_path) -> None:
    fits = ContextFitReport(
        target=None,
        input_count=RequestTokenCount(80, RequestTokenCountMethod.ESTIMATED),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=40,
        decision=ContextFitDecision.FITS,
    )
    rendered, kind = render_context_inspection(
        inspection(tmp_path, fits, None, UserMessage("x"), AssistantText("y"))
    )

    assert kind == "info"
    assert "Source: full committed history" in rendered
    assert "Context ID: ctx-v1-" in rendered
    assert "Full history: 1 turn, 2 items" in rendered
    assert "Effective history: 1 turn, 2 items" in rendered
    assert "Input: 80 tokens (estimated)" in rendered
    assert "Fit: fits" in rendered
    assert "Remaining capacity: 0 tokens" in rendered

    unavailable, kind = render_context_inspection(
        inspection(tmp_path, None, "provider input assessment is unavailable for fake runtime")
    )
    assert kind == "warning"
    assert "Input: unavailable" in unavailable
    assert "Output reserve: unavailable" in unavailable
    assert "Fit: unknown" in unavailable
    assert "Diagnostic: provider input assessment is unavailable for fake runtime" in unavailable


def test_runtime_switch_rendering_distinguishes_fits_unknown_and_rejection() -> None:
    fits = ContextFitReport(
        target=None,
        input_count=RequestTokenCount(80, RequestTokenCountMethod.ESTIMATED),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=40,
        decision=ContextFitDecision.FITS,
    )
    message, kind = render_runtime_switch("Switched", fits, suffix="final guard remains")
    assert kind == "success"
    assert "input=80 (estimated) + reserve=20 <= window=100" in message
    assert "next provider invocation still runs full preflight" in message

    unknown = ContextFitReport(
        target=None,
        input_count=RequestTokenCount.unknown("counter failed safely"),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=40,
        decision=ContextFitDecision.UNKNOWN,
    )
    message, kind = render_runtime_switch("Switched", unknown, suffix="final guard remains")
    assert kind == "warning"
    assert "compatibility not confirmed" in message
    assert "no history was deleted" in message

    exceeded = ContextFitReport(
        target=None,
        input_count=RequestTokenCount(81, RequestTokenCountMethod.EXACT),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=40,
        decision=ContextFitDecision.CONTEXT_EXCEEDED,
    )
    rejected = render_switch_rejection(exceeded)
    assert "Current runtime and profile selection are unchanged" in rejected
    assert "/session new" in rejected
    assert "/compact" not in rejected


def test_resume_rendering_distinguishes_fit_unknown_fake_and_known_rejection(tmp_path) -> None:
    info = SessionInfo(
        session_id="12345678-1234-4234-9234-123456789abc",
        path=tmp_path / "session.jsonl",
        workspace=str(tmp_path),
        workspace_fingerprint="v1-" + "a" * 64,
        created_at="2026-07-18T00:00:00.000000Z",
        record_count=2,
        turn_count=1,
        closed=False,
        binding=BindingSnapshot.fake(),
    )
    fits = ContextFitReport(
        target=None,
        input_count=RequestTokenCount(80, RequestTokenCountMethod.ESTIMATED),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=40,
        decision=ContextFitDecision.FITS,
    )
    fit_result = SessionResumeResult(
        info,
        ResumeEffect.APPLIED,
        CurrentTargetContextAssessment(status(), fits),
        "ctx-v1-" + "a" * 64,
        False,
        LatestUpdateStatus.UPDATED,
    )
    message, kind = render_session_resume(fit_result)
    assert kind == "success"
    assert "input=80 (estimated) + reserve=20 <= window=100" in message

    unknown = ContextFitReport(
        target=None,
        input_count=RequestTokenCount.unknown("counter failed safely"),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=40,
        decision=ContextFitDecision.UNKNOWN,
    )
    unknown_result = SessionResumeResult(
        info,
        ResumeEffect.APPLIED,
        CurrentTargetContextAssessment(status(), unknown),
        "ctx-v1-" + "a" * 64,
        False,
        LatestUpdateStatus.UPDATED,
    )
    message, kind = render_session_resume(unknown_result)
    assert kind == "warning"
    assert "resume was applied" in message
    assert "no history was deleted" in message

    fake_result = SessionResumeResult(
        info,
        ResumeEffect.APPLIED,
        CurrentTargetContextAssessment(status(), None, "unavailable"),
        "ctx-v1-" + "a" * 64,
        False,
        LatestUpdateStatus.UPDATED,
    )
    message, kind = render_session_resume(fake_result)
    assert kind == "warning"
    assert "no provider request was made" in message

    exceeded = ContextFitReport(
        target=None,
        input_count=RequestTokenCount(81, RequestTokenCountMethod.EXACT),
        requested_output_tokens=20,
        context_window_limit=100,
        model_output_limit=40,
        decision=ContextFitDecision.CONTEXT_EXCEEDED,
    )
    rejected = render_resume_rejection(exceeded)
    assert "target transcript" in rejected
    assert "runtime are unchanged" in rejected
    assert "compact" not in rejected.lower()


def test_resume_rendering_reports_same_current_and_latest_partial_outcomes(tmp_path) -> None:
    info = SessionInfo(
        session_id="12345678-1234-4234-9234-123456789abc",
        path=tmp_path / "session.jsonl",
        workspace=str(tmp_path),
        workspace_fingerprint="v1-" + "a" * 64,
        created_at="2026-07-18T00:00:00.000000Z",
        record_count=1,
        turn_count=0,
        closed=False,
        binding=BindingSnapshot.fake(),
    )
    current = SessionResumeResult(
        info,
        ResumeEffect.ALREADY_CURRENT,
        None,
        "ctx-v1-" + "a" * 64,
        False,
        LatestUpdateStatus.UPDATED,
    )
    message, kind = render_session_resume(current)
    assert kind == "info"
    assert "already current" in message
    assert "no resume record" in message

    latest_failed = SessionResumeResult(
        info,
        ResumeEffect.APPLIED_LATEST_FAILED,
        CurrentTargetContextAssessment(status(), None, "unavailable"),
        "ctx-v1-" + "a" * 64,
        True,
        LatestUpdateStatus.FAILED_UNCHANGED,
        "latest failed",
    )
    message, kind = render_session_resume(latest_failed)
    assert kind == "error"
    assert "resume audit is durable" in message
    assert "latest pointer update failed" in message
    assert "crash tail was recovered" in message


def test_auto_compaction_events_render_without_content_leakage() -> None:
    started = AutoCompactionStarted(
        CompactionTrigger.HIGH_WATER,
        "ctx-v1-" + "a" * 64,
        60,
        "estimated",
        20,
        100,
        80,
    )
    result = CompactContextResult(
        "session",
        5,
        "ctx-v1-" + "a" * 64,
        "ctx-v2-" + "b" * 64,
        2,
        2,
        4,
        60,
        30,
        "estimated",
        ContextFitDecision.FITS,
        CompactionTrigger.HIGH_WATER,
    )

    message, kind = render_prompt_event(started)
    assert kind == "info"
    assert "80% high-water" in message
    message, kind = render_prompt_event(
        AutoCompactionCommitted(CompactionTrigger.HIGH_WATER, result)
    )
    assert kind == "success"
    assert "input 60 -> 30" in message
    assert "Full transcript and /history were preserved" in message
    message, kind = render_prompt_event(
        AutoCompactionNotApplied(
            CompactionTrigger.OVERFLOW,
            "candidate did not fit",
            False,
        )
    )
    assert kind == "error"
    assert "original prompt will not be sent" in message


def test_semantic_colors_are_traditional_and_optional() -> None:
    assert render_message("failed", "error", color=True) == f"{RED}failed{RESET}"
    assert render_message("done", "success", color=True) == f"{GREEN}done{RESET}"
    assert render_message("usage", "warning", color=True) == f"{YELLOW}usage{RESET}"
    assert render_message("info", "info", color=True) == f"{BLUE}info{RESET}"
    assert render_message("failed", "error", color=False) == "failed"


def test_colored_readline_prompt_marks_only_nonprinting_sequences() -> None:
    prompt = render_prompt(status(), Info(), color=True, readline=True)

    assert "\001" in prompt and "\002" in prompt
    assert prompt.count("\001") == prompt.count("\002")
    assert "\x1b[" in prompt
    assert prompt.endswith(">\001\x1b[0m\002 ")


def test_colored_non_readline_prompt_has_no_readline_markers() -> None:
    prompt = render_prompt(status(), Info(), color=True, readline=False)

    assert "\x1b[" in prompt
    assert "\001" not in prompt
    assert "\002" not in prompt


def test_action_audit_renders_redacted_command_summary() -> None:
    audit = SimpleNamespace(
        identity=SimpleNamespace(
            tool_name="run_command",
            action=PermissionAction.DANGEROUS,
            arguments=ToolArguments.from_mapping(
                {
                    "argv": ["uv", "run", "pytest", "--token=secret"],
                    "cwd": "tests",
                    "timeout_seconds": 60,
                }
            ),
        ),
        permission_result=PermissionResult(
            PermissionDecision.ALLOW,
            PermissionReason.ALLOWED_DANGEROUS_AUTO,
        ),
        approval_outcome=None,
        status=ActionAuditStatus.SUCCEEDED,
        result_code="command_succeeded",
        requested_sequence=7,
    )

    rendered = render_action_audits((audit,), 20)

    assert "Action #7: run_command" in rendered
    assert "class: dangerous" in rendered
    assert "command: 'uv' (+3 args)" in rendered
    assert "cwd: 'tests'" in rendered
    assert "timeout: 60s" in rendered
    assert "--token=secret" not in rendered
    assert "result: succeeded (command_succeeded)" in rendered
