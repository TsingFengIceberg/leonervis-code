from __future__ import annotations

import pytest

from leonervis_code.agent.loop import AgentLoop
from leonervis_code.core.contracts import TextMessage
from leonervis_code.providers.fake import ScriptedFakeProvider


def test_loop_commits_ordered_history_after_successful_turns() -> None:
    provider = ScriptedFakeProvider(["first reply", "second reply"])
    loop = AgentLoop(provider)

    assert loop.run("first prompt") == "first reply"
    assert loop.run("second prompt") == "second reply"

    first_user = TextMessage(role="user", text="first prompt")
    first_assistant = TextMessage(role="assistant", text="first reply")
    second_user = TextMessage(role="user", text="second prompt")
    second_assistant = TextMessage(role="assistant", text="second reply")
    assert provider.received_histories == (
        (first_user,),
        (first_user, first_assistant, second_user),
    )
    assert loop.history == (first_user, first_assistant, second_user, second_assistant)


def test_loop_does_not_commit_a_failed_provider_turn() -> None:
    provider = ScriptedFakeProvider(["first reply", RuntimeError("provider failed"), "retry reply"])
    loop = AgentLoop(provider)
    loop.run("first prompt")

    with pytest.raises(RuntimeError, match="provider failed"):
        loop.run("failed prompt")

    assert loop.history == (
        TextMessage(role="user", text="first prompt"),
        TextMessage(role="assistant", text="first reply"),
    )

    assert loop.run("retry prompt") == "retry reply"
    assert provider.received_histories[-1] == (
        TextMessage(role="user", text="first prompt"),
        TextMessage(role="assistant", text="first reply"),
        TextMessage(role="user", text="retry prompt"),
    )


def test_history_snapshots_cannot_be_mutated_by_later_turns() -> None:
    provider = ScriptedFakeProvider(["first reply", "second reply"])
    loop = AgentLoop(provider)
    loop.run("first prompt")
    first_request = provider.received_histories[0]

    loop.run("second prompt")

    assert first_request == (TextMessage(role="user", text="first prompt"),)
    assert loop.history is not first_request
