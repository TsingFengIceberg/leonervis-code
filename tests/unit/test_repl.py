from __future__ import annotations

import io
from pathlib import Path

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.cli.repl import (
    complete_command,
    parse_history_count,
    read_prompt,
    render_recent_history,
    run_repl,
)
from leonervis_code.core.contracts import TextMessage
from leonervis_code.providers.fake import ScriptedFakeProvider


class RecordingLoop:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"reply: {prompt}"


class InterruptingInput(io.StringIO):
    def readline(self, size: int = -1) -> str:
        raise KeyboardInterrupt


def test_tab_completion_returns_existing_slash_commands() -> None:
    assert complete_command("/e", 0) == "/exit"
    assert complete_command("/h", 0) == "/help"
    assert complete_command("/h", 1) == "/history"
    assert complete_command("/q", 0) == "/quit"
    assert complete_command("/", 0) == "/help"
    assert complete_command("/", 1) == "/history"
    assert complete_command("/", 2) == "/exit"
    assert complete_command("/", 3) == "/quit"
    assert complete_command("/", 4) is None
    assert complete_command("ordinary prompt", 0) is None


def test_read_prompt_uses_injected_streams_without_readline() -> None:
    output = io.StringIO()

    assert read_prompt(io.StringIO("Hello\n"), output) == "Hello"
    assert output.getvalue() == "leonervis> "


def test_parse_history_count_accepts_positive_integer_only() -> None:
    assert parse_history_count("/history 2") == 2
    assert parse_history_count("/history") is None
    assert parse_history_count("/history 0") is None
    assert parse_history_count("/history -1") is None
    assert parse_history_count("/history 1.5") is None
    assert parse_history_count("/history ٢") is None
    assert parse_history_count("/history 2 extra") is None


def test_render_recent_history_shows_complete_turns_in_chronological_order() -> None:
    history = (
        TextMessage(role="user", text="first prompt"),
        TextMessage(role="assistant", text="first reply"),
        TextMessage(role="user", text="second prompt"),
        TextMessage(role="assistant", text="second reply"),
        TextMessage(role="user", text="third prompt"),
        TextMessage(role="assistant", text="third reply"),
    )

    assert render_recent_history(history, 2) == (
        "User: second prompt\nAssistant: second reply\n\nUser: third prompt\nAssistant: third reply"
    )
    assert render_recent_history((), 2) == "No conversation turns yet."


def test_repl_routes_each_nonblank_prompt_and_prints_banner(tmp_path) -> None:
    loop = RecordingLoop()
    output = io.StringIO()

    status = run_repl(
        loop,
        stdin=io.StringIO("Hello\n   \nWorld\n/exit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    rendered = output.getvalue()
    assert status == 0
    assert loop.prompts == ["Hello", "World"]
    assert rendered.count("LEONERVIS CODE v0.1.0") == 1
    assert "reply: Hello\n" in rendered
    assert "reply: World\n" in rendered


def test_repl_displays_recent_history_without_creating_a_turn(tmp_path) -> None:
    provider = ScriptedFakeProvider(["first reply", "second reply", "third reply"])
    loop = AgentLoop(provider)
    output = io.StringIO()

    run_repl(
        loop,
        stdin=io.StringIO("first prompt\nsecond prompt\nthird prompt\n/history 2\n/exit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    assert output.getvalue().count("User: second prompt\nAssistant: second reply") == 1
    assert output.getvalue().count("User: third prompt\nAssistant: third reply") == 1
    assert "User: first prompt\nAssistant: first reply" not in output.getvalue()
    assert len(provider.received_histories) == 3
    assert len(loop.history) == 6


def test_repl_rejects_invalid_history_commands_without_creating_a_turn(tmp_path) -> None:
    loop = RecordingLoop()
    output = io.StringIO()

    run_repl(
        loop,
        stdin=io.StringIO("/history\n/history 0\n/history two\n/exit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    assert loop.prompts == []
    assert output.getvalue().count("Usage: /history <positive integer>") == 3


def test_repl_keeps_history_for_its_single_loop_lifetime(tmp_path) -> None:
    provider = ScriptedFakeProvider(["first reply", "second reply"])
    output = io.StringIO()

    run_repl(
        AgentLoop(provider),
        stdin=io.StringIO("first prompt\nsecond prompt\n/exit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    assert provider.received_histories[1] == (
        TextMessage(role="user", text="first prompt"),
        TextMessage(role="assistant", text="first reply"),
        TextMessage(role="user", text="second prompt"),
    )

    loop = RecordingLoop()
    output = io.StringIO()

    run_repl(
        loop,
        stdin=io.StringIO("/help\n/unknown\n/quit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    rendered = output.getvalue()
    assert loop.prompts == []
    assert "Commands: /help, /history <count>, /exit, /quit." in rendered
    assert "Unknown command: /unknown. Type /help for controls." in rendered


def test_repl_exits_cleanly_at_end_of_input(tmp_path) -> None:
    output = io.StringIO()

    status = run_repl(
        RecordingLoop(),
        stdin=io.StringIO(),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    assert status == 0
    assert output.getvalue().endswith("leonervis> \n")


def test_repl_exits_cleanly_on_keyboard_interrupt(tmp_path) -> None:
    output = io.StringIO()

    status = run_repl(
        RecordingLoop(),
        stdin=InterruptingInput(),
        stdout=output,
        version="0.1.0",
        cwd=Path(tmp_path),
        color=False,
    )

    assert status == 0
    assert output.getvalue().endswith("leonervis> \n")
