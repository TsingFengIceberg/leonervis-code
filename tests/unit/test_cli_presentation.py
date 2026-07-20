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
)
from leonervis_code.providers.manager import RuntimeStatus


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
