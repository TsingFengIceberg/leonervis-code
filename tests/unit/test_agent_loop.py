from __future__ import annotations

import pytest

from leonervis_code.agent.loop import AgentLoop, ToolLoopLimitError
from leonervis_code.core.contracts import (
    AssistantText,
    CommittedTurn,
    ConversationTurn,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.tools.read_file import ReadFileTool


def test_loop_commits_structured_tool_causality_after_final_text(tmp_path) -> None:
    (tmp_path / "README.md").write_text("Project notes\n", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse(tool_use_id="read-1", name="read_file", path="README.md"),
            AssistantText(text="I read the project notes."),
            AssistantText(text="Second reply"),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path))

    assert loop.run("Read README") == "I read the project notes."
    assert loop.history == (
        UserMessage(text="Read README"),
        ToolUse(tool_use_id="read-1", name="read_file", path="README.md"),
        ToolResult(tool_use_id="read-1", content="Project notes\n"),
        AssistantText(text="I read the project notes."),
    )
    assert loop.turns == (
        ConversationTurn(
            user=UserMessage(text="Read README"),
            assistant=AssistantText(text="I read the project notes."),
        ),
    )

    assert loop.run("Continue") == "Second reply"
    assert provider.received_requests[-1].history == loop.history[:-1]


def test_loop_returns_unknown_tools_as_model_visible_errors(tmp_path) -> None:
    provider = ScriptedFakeProvider(
        [
            ToolUse(tool_use_id="unknown-1", name="search", path="README.md"),
            AssistantText(text="The requested tool is unavailable."),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path))

    assert loop.run("Search") == "The requested tool is unavailable."
    assert provider.received_requests[1].history[-1] == ToolResult(
        tool_use_id="unknown-1", content="unknown tool: search", is_error=True
    )


def test_loop_does_not_commit_candidate_when_provider_fails_after_a_tool(tmp_path) -> None:
    (tmp_path / "README.md").write_text("contents", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse(tool_use_id="read-1", name="read_file", path="README.md"),
            RuntimeError("provider failed"),
            AssistantText(text="retry reply"),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path))

    with pytest.raises(RuntimeError, match="provider failed"):
        loop.run("failed prompt")

    assert loop.history == ()
    assert loop.effective_history == ()
    assert loop.turns == ()
    assert loop.run("retry prompt") == "retry reply"
    assert provider.received_requests[-1].history == (UserMessage(text="retry prompt"),)


def test_loop_bounds_tool_requests_and_returns_budget_error_before_final_text(tmp_path) -> None:
    (tmp_path / "README.md").write_text("contents", encoding="utf-8")
    requests = [
        ToolUse(tool_use_id=f"read-{number}", name="read_file", path="README.md")
        for number in range(1, 5)
    ]
    provider = ScriptedFakeProvider([*requests, AssistantText(text="Finished after the limit.")])
    loop = AgentLoop(provider, ReadFileTool(tmp_path))

    assert loop.run("Read repeatedly") == "Finished after the limit."
    results = [item for item in loop.history if isinstance(item, ToolResult)]
    assert [result.tool_use_id for result in results] == ["read-1", "read-2", "read-3", "read-4"]
    assert results[-1] == ToolResult(
        tool_use_id="read-4",
        content="tool call limit reached for this conversation turn",
        is_error=True,
    )


def test_loop_rejects_another_tool_after_the_limit_without_committing(tmp_path) -> None:
    (tmp_path / "README.md").write_text("contents", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            *[
                ToolUse(tool_use_id=f"read-{number}", name="read_file", path="README.md")
                for number in range(1, 6)
            ]
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path))

    with pytest.raises(ToolLoopLimitError, match="tool call limit"):
        loop.run("Read repeatedly")

    assert loop.history == ()
    assert loop.effective_history == ()
    assert loop.turns == ()


def test_loop_persists_complete_turn_before_memory_commit(tmp_path) -> None:
    committed: list[CommittedTurn] = []
    provider = ScriptedFakeProvider([AssistantText(text="saved")])
    loop = AgentLoop(
        provider,
        ReadFileTool(tmp_path),
        commit_turn=committed.append,
    )

    assert loop.run("persist") == "saved"
    assert committed == [
        CommittedTurn(
            items=(UserMessage("persist"), AssistantText("saved")),
            user=UserMessage("persist"),
            assistant=AssistantText("saved"),
        )
    ]
    assert loop.history == committed[0].items


def test_loop_does_not_commit_memory_when_durable_commit_fails(tmp_path) -> None:
    provider = ScriptedFakeProvider([AssistantText(text="not durable")])

    def fail(_: CommittedTurn) -> None:
        raise OSError("disk full")

    loop = AgentLoop(provider, ReadFileTool(tmp_path), commit_turn=fail)

    with pytest.raises(OSError, match="disk full"):
        loop.run("persist")

    assert loop.history == ()
    assert loop.effective_history == ()
    assert loop.turns == ()


def test_loop_restores_validated_history_and_rejects_broken_causality(tmp_path) -> None:
    restored = (
        UserMessage("read"),
        ToolUse("call-1", "read_file", "README.md"),
        ToolResult("call-1", "notes"),
        AssistantText("done"),
    )
    loop = AgentLoop(None, ReadFileTool(tmp_path), initial_history=restored)

    assert loop.history == restored
    assert loop.effective_history == restored
    assert loop.turns == (ConversationTurn(UserMessage("read"), AssistantText("done")),)

    with pytest.raises(ValueError, match="does not match"):
        AgentLoop(
            None,
            ReadFileTool(tmp_path),
            initial_history=(
                UserMessage("read"),
                ToolUse("call-1", "read_file", "README.md"),
                ToolResult("other", "notes"),
                AssistantText("done"),
            ),
        )


def test_history_snapshots_cannot_be_mutated_by_later_turns(tmp_path) -> None:
    provider = ScriptedFakeProvider(
        [AssistantText(text="first reply"), AssistantText(text="second reply")]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path))
    loop.run("first prompt")
    first_request = provider.received_requests[0].history

    loop.run("second prompt")

    assert first_request == (UserMessage(text="first prompt"),)
    assert loop.history is not first_request


def test_committed_context_snapshot_is_exact_read_only_and_independent(tmp_path) -> None:
    history = (
        UserMessage("read"),
        ToolUse("call-1", "read_file", "README.md"),
        ToolResult("call-1", "notes"),
        AssistantText("done"),
    )
    snapshots = []

    def build_snapshot():
        from leonervis_code.system_prompt import build_system_prompt

        snapshot = build_system_prompt()
        snapshots.append(snapshot)
        return snapshot

    loop = AgentLoop(
        None,
        ReadFileTool(tmp_path),
        initial_history=history,
        system_prompt_factory=build_snapshot,
    )

    request = loop.committed_context_request()

    assert request.history == history
    assert request.history is loop.history
    assert isinstance(request.history[-1], AssistantText)
    assert loop.history == history
    assert loop.turns == (ConversationTurn(UserMessage("read"), AssistantText("done")),)
    assert len(snapshots) == 1


def test_empty_committed_context_has_no_synthetic_user_message(tmp_path) -> None:
    loop = AgentLoop(None, ReadFileTool(tmp_path))

    request = loop.committed_context_request()

    assert request.history == ()
    assert loop.history == ()
    assert loop.effective_history == ()
    assert loop.turns == ()


def test_loop_pins_one_system_prompt_snapshot_across_tool_continuations(tmp_path) -> None:
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse("call-1", "read_file", "README.md"),
            AssistantText("done"),
        ]
    )
    snapshots = []

    def build_snapshot():
        from leonervis_code.system_prompt import build_system_prompt

        snapshot = build_system_prompt()
        snapshots.append(snapshot)
        return snapshot

    loop = AgentLoop(
        provider,
        ReadFileTool(tmp_path),
        system_prompt_factory=build_snapshot,
    )

    assert loop.run("read") == "done"
    assert len(snapshots) == 1
    assert [request.system_prompt for request in provider.received_requests] == [
        snapshots[0],
        snapshots[0],
    ]
    assert (
        provider.received_requests[0].system_prompt is provider.received_requests[1].system_prompt
    )
    assert all(snapshots[0].text not in repr(item) for item in loop.history)
