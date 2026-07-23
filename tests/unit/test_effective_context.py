from __future__ import annotations

from dataclasses import replace

import pytest

from leonervis_code.core.compaction import EffectiveContextSummary
from leonervis_code.core.contracts import (
    AssistantText,
    SystemPromptSnapshot,
    ToolArguments,
    ToolResult,
    ToolUse,
    UserMessage,
)
from leonervis_code.core.effective_context import (
    COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
    EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT,
    EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
    CanonicalToolDefinition,
    EffectiveContextSnapshot,
    validate_complete_history,
)
from leonervis_code.system_prompt import build_system_prompt
from leonervis_code.tools.catalog import TOOL_CATALOG
from leonervis_code.tools.read_file import read_file_model_definition


def snapshot(*history) -> EffectiveContextSnapshot:
    items = tuple(history)
    return EffectiveContextSnapshot(
        representation_version=EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
        source=EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
        system_prompt=build_system_prompt(),
        tool_definitions=TOOL_CATALOG,
        full_history=items,
        effective_history=items,
    )


def test_empty_effective_context_is_stable_and_has_no_synthetic_user() -> None:
    first = snapshot()
    second = snapshot()

    assert first.context_id == second.context_id
    assert (
        first.context_id
        == "ctx-v1-0d8ecf37122888c4bdc22d3a8e3cf9c3922da9c834b8f789d67cb3d9aa2ad730"
    )
    assert first.full_turn_count == first.effective_turn_count == 0
    assert first.full_item_count == first.effective_item_count == 0
    assert first.to_conversation_request().history == ()


def test_complete_tool_turn_is_atomic_and_identity_covers_flags() -> None:
    history = (
        UserMessage("read"),
        ToolUse("call-1", "read_file", ToolArguments.from_mapping({"path": "README.md"})),
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
    changed_arguments = snapshot(
        history[0],
        ToolUse("call-1", "read_file", ToolArguments.from_mapping({"path": "other.md"})),
        history[2],
        history[3],
    )
    assert context.context_id != changed_arguments.context_id


@pytest.mark.parametrize(
    "history, message",
    [
        ((AssistantText("bad"),), "start with a user"),
        ((UserMessage("bad"),), "end with assistant"),
        (
            (
                UserMessage("x"),
                ToolUse("one", "read_file", ToolArguments.from_mapping({"path": "x"})),
                AssistantText("bad"),
            ),
            "does not match",
        ),
        (
            (
                UserMessage("x"),
                ToolUse("one", "read_file", ToolArguments.from_mapping({"path": "x"})),
                ToolUse("two", "read_file", ToolArguments.from_mapping({"path": "y"})),
                ToolResult("two", "y"),
                ToolResult("one", "x"),
                AssistantText("bad"),
            ),
            "does not match",
        ),
        (
            (
                UserMessage("x"),
                ToolUse("one", "read_file", ToolArguments.from_mapping({"path": "x"})),
                ToolResult("one", "x"),
                AssistantText("one"),
                UserMessage("y"),
                ToolUse("one", "read_file", ToolArguments.from_mapping({"path": "y"})),
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
        tool_definitions=(
            CanonicalToolDefinition.from_mapping(tool),
            *TOOL_CATALOG[1:],
        ),
    )
    assert altered_tool.context_id != context.context_id
    assert (
        replace(context, tool_definitions=tuple(reversed(TOOL_CATALOG))).context_id
        != context.context_id
    )
    with pytest.raises(ValueError, match="duplicate"):
        replace(context, tool_definitions=(TOOL_CATALOG[0], TOOL_CATALOG[0]))


def test_full_history_source_requires_transcript_and_effective_equality() -> None:
    full = (UserMessage("one"), AssistantText("reply"))
    with pytest.raises(ValueError, match="must equal"):
        EffectiveContextSnapshot(
            representation_version=1,
            source=EFFECTIVE_CONTEXT_SOURCE_FULL_COMMITTED_HISTORY,
            system_prompt=build_system_prompt(),
            tool_definitions=TOOL_CATALOG,
            full_history=full,
            effective_history=(),
        )


def test_compacted_context_identity_covers_summary_and_retained_suffix() -> None:
    full = (
        UserMessage("one"),
        AssistantText("a"),
        UserMessage("two"),
        AssistantText("b"),
        UserMessage("three"),
        AssistantText("c"),
    )
    summary = EffectiveContextSummary("Earlier: one")
    context = EffectiveContextSnapshot(
        representation_version=COMPACTED_EFFECTIVE_CONTEXT_REPRESENTATION_VERSION,
        source=EFFECTIVE_CONTEXT_SOURCE_COMPACT_CHECKPOINT,
        system_prompt=build_system_prompt(),
        tool_definitions=TOOL_CATALOG,
        full_history=full,
        effective_history=full[-4:],
        effective_summary=summary,
    )

    assert context.context_id.startswith("ctx-v2-")
    assert context.full_turn_count == 3
    assert context.effective_turn_count == 2
    assert context.to_conversation_request().effective_summary == summary
    assert (
        context.context_id
        != replace(context, effective_summary=EffectiveContextSummary("Different")).context_id
    )
    with pytest.raises(ValueError, match="suffix"):
        replace(context, effective_history=full[:4])
