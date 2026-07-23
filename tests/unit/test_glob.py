from __future__ import annotations

import os

import pytest

from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.tools import glob as glob_module
from leonervis_code.tools.glob import (
    GLOB_TRUNCATION_MARKER,
    MAX_GLOB_MATCHES,
    GlobTool,
)


def request(pattern: str) -> ToolUse:
    return ToolUse("glob-1", "glob", ToolArguments.from_mapping({"pattern": pattern}))


def paths(result) -> list[str]:
    return [line for line in result.content.splitlines() if line != "[truncated]"]


def test_glob_matches_components_recursively_and_in_stable_order(tmp_path) -> None:
    for relative in ("z.py", "a.py", "src/b.py", "src/deep/c.py", "src/deep/no.txt"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative, encoding="utf-8")
    tool = GlobTool(tmp_path)

    root = tool.execute(request("*.py"))
    recursive = tool.execute(request("**/*.py"))
    bracket = tool.execute(request("src/[bd]*.py"))

    assert paths(root) == ["a.py", "z.py"]
    assert paths(recursive) == ["a.py", "src/b.py", "src/deep/c.py", "z.py"]
    assert paths(bracket) == ["src/b.py"]
    assert not recursive.is_error and not recursive.truncated


def test_glob_requires_explicit_dot_components_and_ignores_no_gitignore(tmp_path) -> None:
    (tmp_path / ".env").write_text("hidden", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("hidden", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("ci", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("kept.py\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("kept", encoding="utf-8")
    tool = GlobTool(tmp_path)

    assert paths(tool.execute(request("*"))) == ["kept.py"]
    assert paths(tool.execute(request(".*"))) == [".env", ".gitignore"]
    assert paths(tool.execute(request("**/*.py"))) == ["kept.py"]
    assert paths(tool.execute(request(".github/**/*.yml"))) == [".github/workflows/ci.yml"]


def test_glob_returns_regular_files_only_and_never_follows_symlinks(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "inside.py").write_text("inside", encoding="utf-8")
    outside = tmp_path.parent / "outside-glob.py"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "linked-dir").symlink_to(real, target_is_directory=True)
    (tmp_path / "linked-file.py").symlink_to(real / "inside.py")
    (tmp_path / "escape.py").symlink_to(outside)
    (tmp_path / "broken.py").symlink_to(tmp_path / "missing.py")

    result = GlobTool(tmp_path).execute(request("**/*.py"))

    assert paths(result) == ["real/inside.py"]


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        " ",
        "/tmp/*.py",
        "C:/tmp/*.py",
        "\\\\server\\share\\*.py",
        "src\\*.py",
        "../*.py",
        "./*.py",
        "src//*.py",
        "src/",
        "src/**foo/*.py",
        "bad\x00*.py",
    ],
)
def test_glob_rejects_nonportable_or_unsafe_patterns(tmp_path, pattern) -> None:
    result = GlobTool(tmp_path).execute(request(pattern))

    assert result.is_error
    assert result.content.startswith("glob pattern")
    assert str(tmp_path) not in result.content


def test_glob_no_match_is_successful_empty_content(tmp_path) -> None:
    result = GlobTool(tmp_path).execute(request("**/*.py"))

    assert result.content == ""
    assert not result.is_error
    assert not result.truncated


def test_glob_truncates_at_the_exact_match_boundary(tmp_path) -> None:
    for number in range(MAX_GLOB_MATCHES + 1):
        (tmp_path / f"file-{number:03}.py").write_text("x", encoding="utf-8")
    tool = GlobTool(tmp_path)

    truncated = tool.execute(request("*.py"))
    (tmp_path / f"file-{MAX_GLOB_MATCHES:03}.py").unlink()
    exact = tool.execute(request("*.py"))

    assert truncated.truncated and not truncated.is_error
    assert truncated.content.endswith(GLOB_TRUNCATION_MARKER)
    assert len(paths(truncated)) == MAX_GLOB_MATCHES
    assert not exact.truncated
    assert len(paths(exact)) == MAX_GLOB_MATCHES


def test_glob_bounds_utf8_output_without_splitting_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(glob_module, "MAX_GLOB_OUTPUT_BYTES", 32)
    for name in ("一一一.py", "二二二.py", "三三三.py"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    result = GlobTool(tmp_path).execute(request("*.py"))

    assert result.truncated
    assert result.content.endswith(GLOB_TRUNCATION_MARKER)
    assert len(result.content.encode("utf-8")) <= 32
    result.content.encode("utf-8").decode("utf-8")


def test_glob_reports_traversal_directory_and_depth_limits_without_partial_results(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.py").write_text("x", encoding="utf-8")

    monkeypatch.setattr(glob_module, "MAX_GLOB_SCANNED_ENTRIES", 1)
    entry_result = GlobTool(tmp_path).execute(request("**/*.py"))
    assert entry_result.is_error and entry_result.content.startswith("glob traversal")

    monkeypatch.setattr(glob_module, "MAX_GLOB_SCANNED_ENTRIES", 100)
    monkeypatch.setattr(glob_module, "MAX_GLOB_SCANNED_DIRECTORIES", 1)
    directory_result = GlobTool(tmp_path).execute(request("**/*.py"))
    assert directory_result.is_error and directory_result.content.startswith("glob directory")

    monkeypatch.setattr(glob_module, "MAX_GLOB_SCANNED_DIRECTORIES", 100)
    monkeypatch.setattr(glob_module, "MAX_GLOB_DEPTH", 0)
    depth_result = GlobTool(tmp_path).execute(request("**/*.py"))
    assert depth_result.is_error and depth_result.content.startswith("glob depth")


def test_glob_redacts_scandir_failures(tmp_path, monkeypatch) -> None:
    def fail(_):
        raise PermissionError("secret raw path")

    monkeypatch.setattr(os, "scandir", fail)
    result = GlobTool(tmp_path).execute(request("**/*.py"))

    assert result.is_error
    assert result.content == "glob encountered an unreadable directory"
    assert "secret" not in result.content
