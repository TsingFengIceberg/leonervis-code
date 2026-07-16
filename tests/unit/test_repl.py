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
from leonervis_code.core.contracts import AssistantText, ConversationTurn, ToolUse, UserMessage
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.providers.manager import RuntimeStatus
from leonervis_code.providers.profile import NamedProviderProfile
from leonervis_code.providers.definitions import WireProtocol
from leonervis_code.tools.read_file import ReadFileTool


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
    assert complete_command("/", 4) == "/status"
    assert complete_command("/", 5) == "/provider"
    assert complete_command("/", 6) == "/model"
    assert complete_command("/", 7) is None
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
    turns = (
        ConversationTurn(UserMessage("first prompt"), AssistantText("first reply")),
        ConversationTurn(UserMessage("second prompt"), AssistantText("second reply")),
        ConversationTurn(UserMessage("third prompt"), AssistantText("third reply")),
    )

    assert render_recent_history(turns, 2) == (
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


def test_repl_displays_only_completed_turns_without_creating_a_turn(tmp_path) -> None:
    (tmp_path / "README.md").write_text("contents", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse(tool_use_id="read-1", name="read_file", path="README.md"),
            AssistantText(text="first reply"),
            AssistantText(text="second reply"),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path))
    output = io.StringIO()

    run_repl(
        loop,
        stdin=io.StringIO("first prompt\nsecond prompt\n/history 2\n/exit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    rendered = output.getvalue()
    assert "User: first prompt\nAssistant: first reply" in rendered
    assert "User: second prompt\nAssistant: second reply" in rendered
    assert "README.md" not in rendered
    assert "contents" not in rendered
    assert len(provider.received_histories) == 3
    assert len(loop.turns) == 2


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
    provider = ScriptedFakeProvider([AssistantText("first reply"), AssistantText("second reply")])
    output = io.StringIO()

    run_repl(
        AgentLoop(provider, ReadFileTool(tmp_path)),
        stdin=io.StringIO("first prompt\nsecond prompt\n/exit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    assert provider.received_histories[1] == (
        UserMessage(text="first prompt"),
        AssistantText(text="first reply"),
        UserMessage(text="second prompt"),
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


def test_repl_provider_commands_switch_without_entering_model_history(tmp_path) -> None:
    class RecordingSession:
        def __init__(self) -> None:
            self.prompts = []
            self.turns = ()
            self.used = []
            self.models = []

        def status(self):
            return RuntimeStatus(
                mode="real",
                profile="one",
                selection_source="project",
                provider_id="custom",
                protocol="openai_chat_completions",
                selected_model="model-one",
                wire_model="model-one",
                base_url="http://127.0.0.1:11434/v1",
                base_url_source="profile",
                credential_required=False,
                credential_present=False,
            )

        def list_profiles(self):
            return (
                NamedProviderProfile(
                    "one",
                    "custom",
                    WireProtocol.OPENAI_CHAT_COMPLETIONS,
                    "model-one",
                    "http://127.0.0.1:11434/v1",
                ),
            )

        def use_profile(self, name, *, scope):
            self.used.append((name, scope))
            return self.status()

        def set_model(self, model):
            self.models.append(model)
            status = self.status()
            return RuntimeStatus(**{**status.__dict__, "selected_model": model})

        def prompt(self, prompt):
            self.prompts.append(prompt)
            return f"reply: {prompt}"

    session = RecordingSession()
    output = io.StringIO()
    run_repl(
        session,
        stdin=io.StringIO(
            "/status\n/provider list\n/provider current\n/provider use one\n/model model-two\nHello\n/exit\n"
        ),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    rendered = output.getvalue()
    assert session.used == [("one", "project")]
    assert session.models == ["model-two"]
    assert session.prompts == ["Hello"]
    assert "Credential: not required" in rendered
    assert "one: custom/model-one" in rendered
    assert "profile was not modified" in rendered
    assert "reply: Hello" in rendered


def test_invalid_prefix_commands_are_not_treated_as_switches(tmp_path) -> None:
    loop = RecordingLoop()
    output = io.StringIO()

    run_repl(
        loop,
        stdin=io.StringIO("/modelx gpt-5\n/provider usex one\n/exit\n"),
        stdout=output,
        version="0.1.0",
        cwd=tmp_path,
        color=False,
    )

    assert loop.prompts == []
    assert "Unknown command: /modelx gpt-5" in output.getvalue()
    assert "Unknown command: /provider usex one" in output.getvalue()


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
