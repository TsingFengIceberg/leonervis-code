from __future__ import annotations

import pytest

from leonervis_code.agent.loop import AgentLoop


class RecordingProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def respond(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def test_loop_forwards_prompt_once_and_returns_provider_text() -> None:
    provider = RecordingProvider("provider output")

    result = AgentLoop(provider).run("  preserve this prompt  ")

    assert result == "provider output"
    assert provider.prompts == ["  preserve this prompt  "]


def test_loop_does_not_hide_provider_errors() -> None:
    class FailingProvider:
        def respond(self, prompt: str) -> str:
            raise RuntimeError(f"cannot respond to {prompt}")

    with pytest.raises(RuntimeError, match="cannot respond to hello"):
        AgentLoop(FailingProvider()).run("hello")
