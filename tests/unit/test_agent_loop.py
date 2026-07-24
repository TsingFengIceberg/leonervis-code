from __future__ import annotations

from dataclasses import replace

import pytest

from leonervis_code.agent.loop import AgentLoop, ToolLoopLimitError
from leonervis_code.core.compaction import EffectiveContextSummary
from leonervis_code.core.contracts import (
    ToolArguments,
    AssistantText,
    CommittedTurn,
    ConversationTurn,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.tools.glob import GlobTool
from leonervis_code.tools.grep import GrepTool
from leonervis_code.tools.read_file import ReadFileTool


def test_loop_commits_glob_grep_and_read_causality(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse("glob-1", "glob", ToolArguments.from_mapping({"pattern": "src/*.py"})),
            ToolUse(
                "grep-1",
                "grep",
                ToolArguments.from_mapping({"query": "print", "include": "src/*.py"}),
            ),
            ToolUse("read-1", "read_file", ToolArguments.from_mapping({"path": "src/app.py"})),
            AssistantText("found and read"),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

    assert loop.run("find code") == "found and read"
    grep_result = '{"path":"src/app.py","line":1,"text":"print(\'ok\')"}\n'
    assert loop.history == (
        UserMessage("find code"),
        ToolUse("glob-1", "glob", ToolArguments.from_mapping({"pattern": "src/*.py"})),
        ToolResult("glob-1", "src/app.py\n"),
        ToolUse(
            "grep-1",
            "grep",
            ToolArguments.from_mapping({"query": "print", "include": "src/*.py"}),
        ),
        ToolResult("grep-1", grep_result),
        ToolUse("read-1", "read_file", ToolArguments.from_mapping({"path": "src/app.py"})),
        ToolResult("read-1", "print('ok')\n"),
        AssistantText("found and read"),
    )
    assert [
        definition.name for definition in loop.effective_context_snapshot().tool_definitions
    ] == [
        "read_file",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "run_command",
    ]
    assert provider.received_requests[1].history[-1] == ToolResult("glob-1", "src/app.py\n")
    assert provider.received_requests[2].history[-1] == ToolResult("grep-1", grep_result)
    assert provider.received_requests[3].history[-1] == ToolResult("read-1", "print('ok')\n")


def test_loop_counts_glob_and_read_against_one_shared_budget(tmp_path) -> None:
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse("glob-1", "glob", ToolArguments.from_mapping({"pattern": "*.py"})),
            ToolUse("read-1", "read_file", ToolArguments.from_mapping({"path": "a.py"})),
            ToolUse("glob-2", "glob", ToolArguments.from_mapping({"pattern": "*.py"})),
            ToolUse("read-2", "read_file", ToolArguments.from_mapping({"path": "a.py"})),
            AssistantText("bounded"),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

    assert loop.run("inspect") == "bounded"
    results = [item for item in loop.history if isinstance(item, ToolResult)]
    assert [result.tool_use_id for result in results] == ["glob-1", "read-1", "glob-2", "read-2"]
    assert results[-1] == ToolResult(
        "read-2", "tool call limit reached for this conversation turn", is_error=True
    )


def test_loop_commits_structured_tool_causality_after_final_text(tmp_path) -> None:
    (tmp_path / "README.md").write_text("Project notes\n", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse(
                tool_use_id="read-1",
                name="read_file",
                arguments=ToolArguments.from_mapping({"path": "README.md"}),
            ),
            AssistantText(text="I read the project notes."),
            AssistantText(text="Second reply"),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

    assert loop.run("Read README") == "I read the project notes."
    assert loop.history == (
        UserMessage(text="Read README"),
        ToolUse(
            tool_use_id="read-1",
            name="read_file",
            arguments=ToolArguments.from_mapping({"path": "README.md"}),
        ),
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


def test_prepared_turn_is_read_only_and_rebases_the_same_pending_user(tmp_path) -> None:
    provider = ScriptedFakeProvider([AssistantText("done")])
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

    prepared = loop.prepare_turn("pending")

    assert loop.history == ()
    assert prepared.initial_request.history == (prepared.user,)
    summary = EffectiveContextSummary("earlier")
    loop.install_compaction(summary=summary, retained_history=())
    rebased = prepared.rebase(loop.effective_context_snapshot())
    assert rebased.user is prepared.user
    assert rebased.pending_items is prepared.pending_items
    assert rebased.initial_request.history == (prepared.user,)
    assert rebased.initial_request.effective_summary == summary

    assert loop.run_prepared(rebased) == "done"
    assert provider.received_requests[0].history == (prepared.user,)
    assert loop.history == (prepared.user, AssistantText("done"))

    provider = ScriptedFakeProvider(
        [
            ToolUse(
                tool_use_id="unknown-1",
                name="search",
                arguments=ToolArguments.from_mapping({"path": "README.md"}),
            ),
            AssistantText(text="The requested tool is unavailable."),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

    assert loop.run("Search") == "The requested tool is unavailable."
    assert provider.received_requests[1].history[-1] == ToolResult(
        tool_use_id="unknown-1", content="unknown tool: search", is_error=True
    )


def test_loop_does_not_commit_candidate_when_provider_fails_after_a_tool(tmp_path) -> None:
    (tmp_path / "README.md").write_text("contents", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse(
                tool_use_id="read-1",
                name="read_file",
                arguments=ToolArguments.from_mapping({"path": "README.md"}),
            ),
            RuntimeError("provider failed"),
            AssistantText(text="retry reply"),
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

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
        ToolUse(
            tool_use_id=f"read-{number}",
            name="read_file",
            arguments=ToolArguments.from_mapping({"path": "README.md"}),
        )
        for number in range(1, 5)
    ]
    provider = ScriptedFakeProvider([*requests, AssistantText(text="Finished after the limit.")])
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

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
                ToolUse(
                    tool_use_id=f"read-{number}",
                    name="read_file",
                    arguments=ToolArguments.from_mapping({"path": "README.md"}),
                )
                for number in range(1, 6)
            ]
        ]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

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
        GlobTool(tmp_path),
        GrepTool(tmp_path),
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

    loop = AgentLoop(
        provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path), commit_turn=fail
    )

    with pytest.raises(OSError, match="disk full"):
        loop.run("persist")

    assert loop.history == ()
    assert loop.effective_history == ()
    assert loop.turns == ()


def test_loop_restores_validated_history_and_rejects_broken_causality(tmp_path) -> None:
    restored = (
        UserMessage("read"),
        ToolUse("call-1", "read_file", ToolArguments.from_mapping({"path": "README.md"})),
        ToolResult("call-1", "notes"),
        AssistantText("done"),
    )
    loop = AgentLoop(
        None,
        ReadFileTool(tmp_path),
        GlobTool(tmp_path),
        GrepTool(tmp_path),
        initial_history=restored,
    )

    assert loop.history == restored
    assert loop.effective_history == restored
    assert loop.turns == (ConversationTurn(UserMessage("read"), AssistantText("done")),)

    with pytest.raises(ValueError, match="does not match"):
        AgentLoop(
            None,
            ReadFileTool(tmp_path),
            GlobTool(tmp_path),
            GrepTool(tmp_path),
            initial_history=(
                UserMessage("read"),
                ToolUse("call-1", "read_file", ToolArguments.from_mapping({"path": "README.md"})),
                ToolResult("other", "notes"),
                AssistantText("done"),
            ),
        )


def test_history_snapshots_cannot_be_mutated_by_later_turns(tmp_path) -> None:
    provider = ScriptedFakeProvider(
        [AssistantText(text="first reply"), AssistantText(text="second reply")]
    )
    loop = AgentLoop(provider, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))
    loop.run("first prompt")
    first_request = provider.received_requests[0].history

    loop.run("second prompt")

    assert first_request == (UserMessage(text="first prompt"),)
    assert loop.history is not first_request


def test_committed_context_snapshot_is_exact_read_only_and_independent(tmp_path) -> None:
    history = (
        UserMessage("read"),
        ToolUse("call-1", "read_file", ToolArguments.from_mapping({"path": "README.md"})),
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
        GlobTool(tmp_path),
        GrepTool(tmp_path),
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
    loop = AgentLoop(None, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))

    request = loop.committed_context_request()

    assert request.history == ()
    assert loop.history == ()
    assert loop.effective_history == ()
    assert loop.turns == ()


def test_loop_pins_one_system_prompt_snapshot_across_tool_continuations(tmp_path) -> None:
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    provider = ScriptedFakeProvider(
        [
            ToolUse("call-1", "read_file", ToolArguments.from_mapping({"path": "README.md"})),
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
        GlobTool(tmp_path),
        GrepTool(tmp_path),
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


def _action_lease_for(prepared, *, lease_id="12345678-1234-4234-9234-123456789abc"):
    from leonervis_code.core.actions import ActionLease

    return ActionLease(
        session_id="22345678-1234-4234-9234-123456789abc",
        lease_id=lease_id,
        runtime_generation=0,
        context_id=prepared.context.context_id,
    )


def test_prepared_turn_binds_one_action_lease_and_cannot_rebase_after_binding(tmp_path) -> None:
    loop = AgentLoop(None, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))
    prepared = loop.prepare_turn("hello")
    lease = _action_lease_for(prepared)

    leased = prepared.with_action_lease(lease)

    assert leased.action_lease == lease
    with pytest.raises(ValueError, match="already has"):
        leased.with_action_lease(lease)
    with pytest.raises(ValueError, match="cannot be rebased"):
        leased.rebase(loop.effective_context_snapshot())
    with pytest.raises(ValueError, match="context does not match"):
        prepared.with_action_lease(replace(lease, context_id=f"ctx-v1-{'0' * 64}"))


def test_action_dispatcher_receives_the_same_lease_across_tool_continuations(tmp_path) -> None:
    first = ToolUse("read-1", "read_file", ToolArguments.from_mapping({"path": "a.txt"}))
    second = ToolUse("glob-1", "glob", ToolArguments.from_mapping({"pattern": "*.txt"}))
    provider = ScriptedFakeProvider([first, second, AssistantText("done")])
    loop = AgentLoop(None, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))
    prepared = loop.prepare_turn("inspect")
    lease = _action_lease_for(prepared)
    received = []

    def dispatch(request, current_lease):
        received.append((request, current_lease))
        return ToolResult(request.tool_use_id, f"resolved {request.name}")

    loop.install_action_dispatcher(dispatch)

    assert loop.run_prepared(prepared.with_action_lease(lease), provider=provider) == "done"
    assert received == [(first, lease), (second, lease)]
    assert provider.received_requests[1].history[-1] == ToolResult("read-1", "resolved read_file")
    assert provider.received_requests[2].history[-1] == ToolResult("glob-1", "resolved glob")


def test_fourth_tool_call_gets_limit_result_without_entering_action_dispatch(tmp_path) -> None:
    calls = [
        ToolUse(
            f"read-{index}",
            "read_file",
            ToolArguments.from_mapping({"path": f"{index}.txt"}),
        )
        for index in range(1, 5)
    ]
    provider = ScriptedFakeProvider([*calls, AssistantText("stopped")])
    loop = AgentLoop(None, ReadFileTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path))
    prepared = loop.prepare_turn("inspect")
    lease = _action_lease_for(prepared)
    dispatched = []

    def dispatch(request, _lease):
        dispatched.append(request.tool_use_id)
        return ToolResult(request.tool_use_id, "permission denied", is_error=True)

    loop.install_action_dispatcher(dispatch)

    assert loop.run_prepared(prepared.with_action_lease(lease), provider=provider) == "stopped"
    assert dispatched == ["read-1", "read-2", "read-3"]
    assert provider.received_requests[-1].history[-1] == ToolResult(
        "read-4",
        "tool call limit reached for this conversation turn",
        is_error=True,
    )
