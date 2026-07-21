from __future__ import annotations

from dataclasses import replace

import pytest

from leonervis_code.core.contracts import (
    AssistantText,
    SystemPromptSnapshot,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.core.effective_context import (
    EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
    CanonicalToolDefinition,
    EffectiveContextSnapshot,
    validate_complete_history,
)
from leonervis_code.system_prompt import build_system_prompt
from leonervis_code.tools.read_file import read_file_model_definition, read_file_tool_snapshot


def snapshot(*history) -> EffectiveContextSnapshot:
    items = tuple(history)
    return EffectiveContextSnapshot(
        representation_version=EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
        source=EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
        system_prompt=build_system_prompt(),
        tool_definitions=(read_file_tool_snapshot(),),
        full_history=items,
        effective_history=items,
    )


def test_empty_effective_context_is_stable_and_has_no_synthetic_user() -> None:
    first = snapshot()
    second = snapshot()

    assert first.context_id == second.context_id
    assert (
        first.context_id
        == "ctx-v1-b1c9472533f3d87a8cbb8558545da88b72f1e27d96e005923d32501fae85b85e"
    )
    assert first.full_turn_count == first.effective_turn_count == 0
    assert first.full_item_count == first.effective_item_count == 0
    assert first.to_conversation_request().history == ()


def test_complete_tool_turn_is_atomic_and_identity_covers_flags() -> None:
    history = (
        UserMessage("read"),
        ToolUse("call-1", "read_file", "README.md"),
        ToolResult("call-1", "notes", is_error=False, truncated=False),
        AssistantText("done"),
    )
    context = snapshot(*history)
    changed = snapshot(
        history[0],
        history[1],
        replace(history[2], truncated=True),
        history[3],
    )

    assert context.full_turn_count == 1
    assert context.full_item_count == 4
    assert context.effective_turns[0].items == history
    assert context.context_id != changed.context_id


@pytest.mark.parametrize(
    "history, message",
    [
        ((AssistantText("bad"),), "start with a user"),
        ((UserMessage("bad"),), "end with assistant"),
        (
            (UserMessage("x"), ToolUse("one", "read_file", "x"), AssistantText("bad")),
            "does not match",
        ),
        (
            (
                UserMessage("x"),
                ToolUse("one", "read_file", "x"),
                ToolUse("two", "read_file", "y"),
                ToolResult("two", "y"),
                ToolResult("one", "x"),
                AssistantText("bad"),
            ),
            "does not match",
        ),
        (
            (
                UserMessage("x"),
                ToolUse("one", "read_file", "x"),
                ToolResult("one", "x"),
                AssistantText("one"),
                UserMessage("y"),
                ToolUse("one", "read_file", "y"),
                ToolResult("one", "y"),
                AssistantText("two"),
            ),
            "duplicate tool use ID",
        ),
    ],
)
def test_complete_history_fails_closed_on_invalid_causality(history, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_complete_history(history)


def test_context_identity_includes_prompt_and_tool_contract() -> None:
    context = snapshot(UserMessage("hello"), AssistantText("reply"))
    with pytest.raises(ValueError, match="fingerprint"):
        replace(
            context,
            system_prompt=SystemPromptSnapshot(
                version=1,
                text="different\n",
                fingerprint="v1-invalid",
            ),
        )

    tool = read_file_model_definition()
    tool["description"] = "different"
    altered_tool = replace(
        context,
        tool_definitions=(CanonicalToolDefinition.from_mapping(tool),),
    )
    assert altered_tool.context_id != context.context_id


def test_full_history_source_requires_transcript_and_effective_equality() -> None:
    full = (UserMessage("one"), AssistantText("reply"))
    with pytest.raises(ValueError, match="must equal"):
        EffectiveContextSnapshot(
            representation_version=1,
            source=EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
            system_prompt=build_system_prompt(),
            tool_definitions=(read_file_tool_snapshot(),),
            full_history=full,
            effective_history=(),
        )
