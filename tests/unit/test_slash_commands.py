from __future__ import annotations

from dataclasses import dataclass

from leonervis_code.cli.slash import dispatch_slash
from leonervis_code.providers.manager import RuntimeStatus, RuntimeSwitchResult
from leonervis_code.session_records import BindingSnapshot
from leonervis_code.session_store import SessionInfo


@dataclass
class Text:
    text: str


@dataclass
class Turn:
    user: Text
    assistant: Text


@dataclass
class Profile:
    name: str = "one"
    provider_id: str = "custom"
    model: str = "model-one"


class Session:
    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.turns = (Turn(Text("hello"), Text("reply")),)
        self.current = "12345678-1234-4234-9234-123456789abc"
        self.latest = self.current
        self.prompts = []

    def status(self):
        return RuntimeStatus(
            mode="fake",
            profile=None,
            selection_source="default",
            provider_id="fake",
            protocol=None,
            selected_model=None,
            wire_model=None,
            base_url=None,
            base_url_source=None,
            credential_required=False,
            credential_present=False,
        )

    def _info(self, session_id):
        return SessionInfo(
            session_id=session_id,
            path=self.tmp_path / f"{session_id}.jsonl",
            workspace=str(self.tmp_path),
            workspace_fingerprint="v1-" + "a" * 64,
            created_at="2026-07-18T00:00:00.000000Z",
            record_count=1,
            turn_count=1,
            closed=False,
            binding=BindingSnapshot.fake(),
        )

    def session_info(self):
        return self._info(self.current)

    def latest_session_info(self):
        return self._info(self.latest)

    def list_sessions(self):
        return (self.session_info(),)

    def new_session(self):
        self.current = "22345678-1234-4234-9234-123456789abc"
        self.latest = self.current
        return self.session_info()

    def switch_session(self, selector):
        self.current = selector
        self.latest = selector
        return self.session_info()

    def list_profiles(self):
        return (Profile(),)

    def use_profile(self, name, *, scope):
        status = RuntimeStatus(**{**self.status().__dict__, "mode": "real", "profile": name})
        return RuntimeSwitchResult(status, None)

    def set_model(self, model):
        status = RuntimeStatus(**{**self.status().__dict__, "selected_model": model})
        return RuntimeSwitchResult(status, None)

    def prompt(self, text):
        self.prompts.append(text)


def test_group_help_and_targeted_usage(tmp_path) -> None:
    session = Session(tmp_path)

    assert "Session commands:" in dispatch_slash("/session", session).message
    assert "Provider commands:" in dispatch_slash("/provider", session).message
    unknown = dispatch_slash("/session wat", session)
    assert unknown.kind == "warning"
    assert unknown.message == ("Unknown session command: wat\nUsage: /session <show|list|new>")
    assert dispatch_slash("/session show extra", session).message == "Usage: /session show"
    assert dispatch_slash("/provider use", session).message == "Usage: /provider use <name>"
    assert dispatch_slash("/status extra", session).message == "Usage: /status"


def test_valid_session_commands_do_not_enter_model_history(tmp_path) -> None:
    session = Session(tmp_path)

    created = dispatch_slash("/session new", session)
    resumed = dispatch_slash("/resume 32345678-1234-4234-9234-123456789abc", session)

    assert created.kind == "success"
    assert "runtime provider unchanged" in created.message
    assert resumed.kind == "success"
    assert session.current == "32345678-1234-4234-9234-123456789abc"
    assert session.prompts == []


def test_valid_provider_commands_and_history(tmp_path) -> None:
    session = Session(tmp_path)

    assert "one: custom/model-one" in dispatch_slash("/provider list", session).message
    assert dispatch_slash("/provider use one", session).kind == "success"
    assert dispatch_slash("/model model-two", session).kind == "success"
    history = dispatch_slash("/history 1", session)
    assert history.message == "User: hello\nAssistant: reply"
    assert dispatch_slash("/history 0", session).kind == "warning"
    assert session.prompts == []


def test_prefixes_remain_unknown_top_level_or_group_commands(tmp_path) -> None:
    session = Session(tmp_path)

    assert "Unknown command: /modelx one" in dispatch_slash("/modelx one", session).message
    group = dispatch_slash("/provider usex one", session)
    assert "Unknown provider command: usex" in group.message
    assert session.prompts == []


def test_non_slash_text_is_not_handled(tmp_path) -> None:
    result = dispatch_slash("hello", Session(tmp_path))

    assert not result.handled
    assert not result.exit
    assert result.message is None
