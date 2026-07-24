from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from leonervis_code.core.actions import ActionPreconditionKind
from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.core.permissions import PermissionAction
from leonervis_code.tools.mkdir import (
    MAX_MKDIR_COMPONENT_BYTES,
    MAX_MKDIR_PATH_BYTES,
    MAX_MKDIR_PATH_CHARACTERS,
    MAX_MKDIR_PATH_COMPONENTS,
    MkdirOutcome,
    MkdirPreparationError,
    MkdirTool,
    mkdir_model_definition,
)


def request(path: object = "pkg", *, tool_use_id: str = "mkdir-1") -> ToolUse:
    return ToolUse(
        tool_use_id,
        "mkdir",
        ToolArguments.from_mapping({"path": path}),
    )


def test_prepare_is_side_effect_free_and_execute_creates_one_directory(tmp_path: Path) -> None:
    tool = MkdirTool(tmp_path)

    prepared = tool.prepare(request("src"))

    assert prepared.relative_path == "src"
    assert prepared.action == PermissionAction.WORKSPACE_CREATE
    assert prepared.precondition.kind == ActionPreconditionKind.PATH_ABSENT
    assert not (tmp_path / "src").exists()

    result = tool.execute_detailed(prepared)

    assert result.outcome == MkdirOutcome.SUCCEEDED
    assert result.result_code == "directory_created"
    assert result.tool_result.content == '{"operation":"created","path":"src"}\n'
    assert not result.tool_result.is_error
    assert (tmp_path / "src").is_dir()


def test_prepared_mkdir_is_immutable(tmp_path: Path) -> None:
    prepared = MkdirTool(tmp_path).prepare(request("src"))

    with pytest.raises(FrozenInstanceError):
        prepared.relative_path = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "src", "extra": "x"},
        {"path": 1},
    ],
)
def test_prepare_rejects_malformed_arguments(tmp_path: Path, arguments: dict[str, object]) -> None:
    call = ToolUse("mkdir-1", "mkdir", ToolArguments.from_mapping(arguments))

    with pytest.raises(MkdirPreparationError, match="input is malformed"):
        MkdirTool(tmp_path).prepare(call)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "/absolute",
        "../escape",
        "a/../escape",
        "a/./child",
        "a//child",
        "a/",
        "a\\child",
        "C:/child",
        "nul\x00path",
    ],
)
def test_prepare_rejects_nonportable_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(MkdirPreparationError, match="portable workspace-relative"):
        MkdirTool(tmp_path).prepare(request(path))


def test_prepare_enforces_character_byte_component_and_component_byte_bounds(
    tmp_path: Path,
) -> None:
    tool = MkdirTool(tmp_path)

    with pytest.raises(MkdirPreparationError, match="portable workspace-relative"):
        tool.prepare(request("a" * (MAX_MKDIR_PATH_CHARACTERS + 1)))
    with pytest.raises(MkdirPreparationError, match="portable workspace-relative"):
        tool.prepare(request("é" * (MAX_MKDIR_PATH_BYTES // 2 + 1)))
    too_many = "/".join("a" for _ in range(MAX_MKDIR_PATH_COMPONENTS + 1))
    with pytest.raises(MkdirPreparationError, match="portable workspace-relative"):
        tool.prepare(request(too_many))
    with pytest.raises(MkdirPreparationError, match="component exceeds"):
        tool.prepare(request("é" * (MAX_MKDIR_COMPONENT_BYTES // 2 + 1)))


def test_prepare_requires_existing_real_parent_directory(tmp_path: Path) -> None:
    (tmp_path / "file-parent").write_text("x", encoding="utf-8")
    tool = MkdirTool(tmp_path)

    with pytest.raises(MkdirPreparationError, match="parent directory does not exist"):
        tool.prepare(request("missing/child"))
    with pytest.raises(MkdirPreparationError, match="parent path is not a directory"):
        tool.prepare(request("file-parent/child"))

    assert not (tmp_path / "missing").exists()


def test_prepare_rejects_intermediate_and_final_symlinks_and_existing_targets(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "dir-link").symlink_to(outside, target_is_directory=True)
    (tmp_path / "final-link").symlink_to(outside / "missing", target_is_directory=True)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    (tmp_path / "existing").mkdir()
    tool = MkdirTool(tmp_path)

    with pytest.raises(MkdirPreparationError, match="contains a symbolic link"):
        tool.prepare(request("dir-link/child"))
    for path in ("final-link", "file", "existing"):
        with pytest.raises(MkdirPreparationError, match="target already exists"):
            tool.prepare(request(path))

    assert not (outside / "child").exists()


def test_nested_creation_requires_only_the_direct_parent_and_is_not_recursive(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    tool = MkdirTool(tmp_path)

    result = tool.execute_detailed(tool.prepare(request("src/pkg")))

    assert result.outcome == MkdirOutcome.SUCCEEDED
    assert (tmp_path / "src" / "pkg").is_dir()
    with pytest.raises(MkdirPreparationError, match="parent directory does not exist"):
        tool.prepare(request("missing/deeper/pkg"))
    assert not (tmp_path / "missing").exists()


def test_refresh_precondition_detects_a_new_target(tmp_path: Path) -> None:
    tool = MkdirTool(tmp_path)
    prepared = tool.prepare(request("src"))

    assert tool.refresh_precondition(prepared) == prepared.precondition
    (tmp_path / "src").mkdir()

    refreshed = tool.refresh_precondition(prepared)
    assert refreshed.kind == ActionPreconditionKind.EXPECTED_STATE_SHA256
    assert refreshed.fingerprint is not None


def test_execute_rejects_stale_target_without_modifying_it(tmp_path: Path) -> None:
    tool = MkdirTool(tmp_path)
    prepared = tool.prepare(request("src"))
    (tmp_path / "src").mkdir()
    marker = tmp_path / "src" / "external.txt"
    marker.write_text("external\n", encoding="utf-8")

    result = tool.execute_detailed(prepared)

    assert result.outcome == MkdirOutcome.FAILED
    assert result.result_code == "directory_not_created"
    assert result.tool_result.is_error
    assert "conflict" in result.tool_result.content
    assert marker.read_text(encoding="utf-8") == "external\n"


def test_execute_maps_create_race_to_stable_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = MkdirTool(tmp_path)
    prepared = tool.prepare(request("src"))

    def race(_path: Path, *args, **kwargs) -> None:
        raise FileExistsError

    monkeypatch.setattr(Path, "mkdir", race)

    result = tool.execute_detailed(prepared)

    assert result.outcome == MkdirOutcome.FAILED
    assert result.result_code == "directory_not_created"
    assert "conflict" in result.tool_result.content


def test_execute_maps_permission_failure_without_claiming_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = MkdirTool(tmp_path)
    prepared = tool.prepare(request("src"))

    def deny(_path: Path, *args, **kwargs) -> None:
        raise PermissionError

    monkeypatch.setattr(Path, "mkdir", deny)

    result = tool.execute_detailed(prepared)

    assert result.outcome == MkdirOutcome.FAILED
    assert result.result_code == "directory_not_created"
    assert result.tool_result.content == "mkdir target is not writable"
    assert not (tmp_path / "src").exists()


def test_directory_fsync_failure_reports_visible_partial_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = MkdirTool(tmp_path)
    prepared = tool.prepare(request("src"))

    def fail_fsync(_directory: Path) -> None:
        raise OSError("injected")

    monkeypatch.setattr("leonervis_code.tools.mkdir._fsync_directory", fail_fsync)

    result = tool.execute_detailed(prepared)

    assert result.outcome == MkdirOutcome.PARTIAL
    assert result.result_code == "directory_created_durability_unknown"
    assert result.tool_result.is_error
    assert "do not retry automatically" in result.tool_result.content
    assert (tmp_path / "src").is_dir()


def test_execute_rejects_non_absent_precondition(tmp_path: Path) -> None:
    tool = MkdirTool(tmp_path)
    prepared = tool.prepare(request("src"))
    (tmp_path / "src").mkdir()
    invalid = replace(prepared, precondition=tool.refresh_precondition(prepared))
    (tmp_path / "src").rmdir()

    result = tool.execute_detailed(invalid)

    assert result.outcome == MkdirOutcome.FAILED
    assert result.result_code == "directory_not_created"
    assert result.tool_result.content == "mkdir precondition is invalid"
    assert not (tmp_path / "src").exists()


def test_model_definition_is_closed_and_exact() -> None:
    assert mkdir_model_definition() == {
        "name": "mkdir",
        "description": (
            "Create exactly one missing workspace-relative directory. The parent must already "
            "exist. The Host applies workspace-create permission and approval policy, rejects "
            "symlinks and stale targets, and does not create parent directories recursively."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Portable workspace-relative path of the directory to create.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }
