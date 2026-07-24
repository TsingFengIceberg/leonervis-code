from __future__ import annotations

import pytest

from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.tools.catalog import (
    TOOL_CATALOG,
    tool_input_from_use,
    tool_use_from_input,
)


def test_catalog_exposes_edit_file_last_with_shared_closed_schema() -> None:
    assert [definition.name for definition in TOOL_CATALOG] == [
        "read_file",
        "glob",
        "grep",
        "write_file",
        "edit_file",
    ]
    request = tool_use_from_input(
        "edit-1",
        "edit_file",
        {"path": "note.txt", "old_text": " \n", "new_text": ""},
    )
    assert request == ToolUse(
        "edit-1",
        "edit_file",
        ToolArguments.from_mapping({"path": "note.txt", "old_text": " \n", "new_text": ""}),
    )
    assert tool_input_from_use(request) == {
        "path": "note.txt",
        "old_text": " \n",
        "new_text": "",
    }


@pytest.mark.parametrize(
    "tool_input",
    [
        {"path": "note.txt", "old_text": "", "new_text": "after"},
        {"path": "note.txt", "old_text": "before"},
        {"path": "note.txt", "old_text": "before", "new_text": "after", "extra": "x"},
        {"path": "note.txt", "old_text": "before", "new_text": 1},
    ],
)
def test_catalog_rejects_malformed_edit_file_inputs(tool_input: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="edit_file"):
        tool_use_from_input("edit-1", "edit_file", tool_input)
