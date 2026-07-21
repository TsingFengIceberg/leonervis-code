from __future__ import annotations

from dataclasses import dataclass

from leonervis_code.cli.presentation import (
    BLUE,
    GREEN,
    RED,
    RESET,
    YELLOW,
    render_message,
    render_prompt,
    render_runtime_status,
    render_runtime_switch,
    render_switch_rejection,
)
from leonervis_code.providers.manager import RuntimeStatus
from leonervis_code.providers.request_context import (
    ContextFitDecision,
    ContextFitReport,
    RequestTokenCount,
    RequestTokenCountMethod,
)


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
