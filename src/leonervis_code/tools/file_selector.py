"""Shared portable bounded regular-file selection for read-only tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
import os
from pathlib import Path, PureWindowsPath

MAX_PATTERN_CHARACTERS = 4096
MAX_PATTERN_BYTES = 4096
MAX_PATTERN_COMPONENTS = 64
MAX_SCANNED_ENTRIES = 10_000
MAX_SCANNED_DIRECTORIES = 1_000
MAX_DEPTH = 32


class SelectorFailureKind(StrEnum):
    BLANK_PATTERN = "blank_pattern"
    INVALID_COMPONENT = "invalid_component"
    OVERSIZED_PATTERN = "oversized_pattern"
    NONPORTABLE_PATTERN = "nonportable_pattern"
    DIRECTORY_LIMIT = "directory_limit"
    UNREADABLE_DIRECTORY = "unreadable_directory"
    SCAN_FAILED = "scan_failed"
    INVALID_UTF8_PATH = "invalid_utf8_path"
    ENTRY_LIMIT = "entry_limit"
    DEPTH_LIMIT = "depth_limit"
    FILE_LIMIT = "file_limit"


class FileSelectionFailure(RuntimeError):
    """Typed failure that callers translate to tool-specific diagnostics."""

    def __init__(self, kind: SelectorFailureKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


@dataclass(frozen=True)
class SelectorLimits:
    """One immutable set of traversal bounds for a file selection."""

    max_pattern_characters: int = MAX_PATTERN_CHARACTERS
    max_pattern_bytes: int = MAX_PATTERN_BYTES
    max_pattern_components: int = MAX_PATTERN_COMPONENTS
    max_scanned_entries: int = MAX_SCANNED_ENTRIES
    max_scanned_directories: int = MAX_SCANNED_DIRECTORIES
    max_depth: int = MAX_DEPTH


@dataclass(frozen=True)
class SelectedFile:
    """One stable workspace-relative regular-file candidate."""

    relative_path: str
    path: Path


def select_files(
    workspace: Path,
    pattern: str,
    *,
    max_files: int | None = None,
    limits: SelectorLimits = SelectorLimits(),
) -> list[SelectedFile]:
    """Select every bounded matching regular file in deterministic order."""
    components = validate_pattern(pattern, limits=limits)
    matches: dict[str, Path] = {}
    scanned_entries = 0
    scanned_directories = 0

    def walk(directory: Path, relative: tuple[str, ...], depth: int) -> None:
        nonlocal scanned_entries, scanned_directories
        scanned_directories += 1
        if scanned_directories > limits.max_scanned_directories:
            raise FileSelectionFailure(SelectorFailureKind.DIRECTORY_LIMIT)
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except PermissionError:
            raise FileSelectionFailure(SelectorFailureKind.UNREADABLE_DIRECTORY) from None
        except OSError:
            raise FileSelectionFailure(SelectorFailureKind.SCAN_FAILED) from None

        try:
            entries.sort(key=lambda entry: entry.name.encode("utf-8"))
        except UnicodeEncodeError:
            raise FileSelectionFailure(SelectorFailureKind.INVALID_UTF8_PATH) from None

        for entry in entries:
            scanned_entries += 1
            if scanned_entries > limits.max_scanned_entries:
                raise FileSelectionFailure(SelectorFailureKind.ENTRY_LIMIT)
            name = entry.name
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                raise FileSelectionFailure(SelectorFailureKind.INVALID_UTF8_PATH) from None
            candidate = relative + (name,)
            try:
                is_file = entry.is_file(follow_symlinks=False)
                is_directory = entry.is_dir(follow_symlinks=False)
            except PermissionError:
                raise FileSelectionFailure(SelectorFailureKind.UNREADABLE_DIRECTORY) from None
            except OSError:
                raise FileSelectionFailure(SelectorFailureKind.SCAN_FAILED) from None

            if is_file and matches_pattern(components, candidate):
                relative_path = "/".join(candidate)
                matches[relative_path] = Path(entry.path)
                if max_files is not None and len(matches) > max_files:
                    raise FileSelectionFailure(SelectorFailureKind.FILE_LIMIT)
            if not is_directory or not can_match_descendant(components, candidate):
                continue
            child_depth = depth + 1
            if child_depth > limits.max_depth:
                raise FileSelectionFailure(SelectorFailureKind.DEPTH_LIMIT)
            walk(Path(entry.path), candidate, child_depth)

    walk(workspace, (), 0)
    return [
        SelectedFile(relative_path, matches[relative_path])
        for relative_path in sorted(matches, key=lambda path: path.encode("utf-8"))
    ]


def validate_pattern(
    pattern: str,
    *,
    limits: SelectorLimits = SelectorLimits(),
) -> tuple[str, ...]:
    """Validate one portable component pattern and return its components."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise FileSelectionFailure(SelectorFailureKind.BLANK_PATTERN)
    if "\x00" in pattern:
        raise FileSelectionFailure(SelectorFailureKind.INVALID_COMPONENT)
    try:
        encoded = pattern.encode("utf-8")
    except UnicodeEncodeError:
        raise FileSelectionFailure(SelectorFailureKind.INVALID_COMPONENT) from None
    if len(pattern) > limits.max_pattern_characters or len(encoded) > limits.max_pattern_bytes:
        raise FileSelectionFailure(SelectorFailureKind.OVERSIZED_PATTERN)
    if pattern.startswith("/") or "\\" in pattern or PureWindowsPath(pattern).drive:
        raise FileSelectionFailure(SelectorFailureKind.NONPORTABLE_PATTERN)

    components = tuple(pattern.split("/"))
    if len(components) > limits.max_pattern_components:
        raise FileSelectionFailure(SelectorFailureKind.OVERSIZED_PATTERN)
    if any(
        component in {"", ".", ".."} or ("**" in component and component != "**")
        for component in components
    ):
        raise FileSelectionFailure(SelectorFailureKind.INVALID_COMPONENT)
    return components


def matches_pattern(pattern: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
    return len(pattern) in _states_after(pattern, candidate)


def can_match_descendant(pattern: tuple[str, ...], directory: tuple[str, ...]) -> bool:
    return any(state < len(pattern) for state in _states_after(pattern, directory))


def _states_after(pattern: tuple[str, ...], candidate: tuple[str, ...]) -> frozenset[int]:
    states = _epsilon_closure(pattern, {0})
    for name in candidate:
        next_states: set[int] = set()
        for state in states:
            if state == len(pattern):
                continue
            component = pattern[state]
            if component == "**":
                if not name.startswith("."):
                    next_states.add(state)
            elif _component_matches(component, name):
                next_states.add(state + 1)
        states = _epsilon_closure(pattern, next_states)
        if not states:
            break
    return frozenset(states)


def _epsilon_closure(pattern: tuple[str, ...], states: set[int]) -> set[int]:
    closed = set(states)
    pending = list(states)
    while pending:
        state = pending.pop()
        if state < len(pattern) and pattern[state] == "**" and state + 1 not in closed:
            closed.add(state + 1)
            pending.append(state + 1)
    return closed


def _component_matches(pattern: str, candidate: str) -> bool:
    if candidate.startswith(".") and not pattern.startswith("."):
        return False
    return fnmatchcase(candidate, pattern)
