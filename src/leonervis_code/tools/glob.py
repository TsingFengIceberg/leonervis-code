"""Deterministic bounded workspace file matching for Foundation 1C."""

from __future__ import annotations

from fnmatch import fnmatchcase
import os
from pathlib import Path, PureWindowsPath

from leonervis_code.core.contracts import ToolResult, ToolUse
from leonervis_code.core.effective_context import CanonicalToolDefinition

GLOB_TOOL_NAME = "glob"
MAX_GLOB_PATTERN_CHARACTERS = 4096
MAX_GLOB_PATTERN_BYTES = 4096
MAX_GLOB_PATTERN_COMPONENTS = 64
MAX_GLOB_MATCHES = 200
MAX_GLOB_OUTPUT_BYTES = 32 * 1024
MAX_GLOB_SCANNED_ENTRIES = 10_000
MAX_GLOB_SCANNED_DIRECTORIES = 1_000
MAX_GLOB_DEPTH = 32
GLOB_TRUNCATION_MARKER = "[truncated]\n"


class _GlobFailure(RuntimeError):
    """One stable model-visible search failure."""


def glob_model_definition() -> dict[str, object]:
    """Return a fresh provider-neutral definition of the bounded glob tool."""
    return {
        "name": GLOB_TOOL_NAME,
        "description": (
            "Match regular files using one portable workspace-relative '/'-separated glob "
            "pattern when file names or locations are needed before choosing files to read. "
            "This tool is read-only, bounded, deterministic, and never returns or traverses "
            "symbolic links. It supports component *, ?, bracket classes, and a whole-component "
            "**. Leading-dot names require an explicit leading dot in the matching component."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Portable workspace-relative '/'-separated regular-file glob pattern."
                    ),
                }
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    }


def glob_tool_snapshot() -> CanonicalToolDefinition:
    """Freeze the canonical neutral glob tool for effective-context identity."""
    return CanonicalToolDefinition.from_mapping(glob_model_definition())


class GlobTool:
    """Match regular files without leaving, mutating, or linking through one workspace."""

    def __init__(self, workspace: Path) -> None:
        """Resolve and validate the directory that bounds every search."""
        self._workspace = workspace.resolve()
        if not self._workspace.is_dir():
            raise ValueError("workspace must be an existing directory")

    def execute(self, request: ToolUse) -> ToolResult:
        """Return bounded stable matches for the request's single pattern operand."""
        try:
            components = _validate_pattern(request.path)
            matches = self._collect_matches(components)
            content, truncated = _format_matches(matches)
        except _GlobFailure as error:
            return self._error(request, str(error))
        return ToolResult(
            tool_use_id=request.tool_use_id,
            content=content,
            truncated=truncated,
        )

    def _collect_matches(self, components: tuple[str, ...]) -> list[str]:
        matches: set[str] = set()
        scanned_entries = 0
        scanned_directories = 0

        def walk(directory: Path, relative: tuple[str, ...], depth: int) -> None:
            nonlocal scanned_entries, scanned_directories
            scanned_directories += 1
            if scanned_directories > MAX_GLOB_SCANNED_DIRECTORIES:
                raise _GlobFailure("glob directory limit reached; use a narrower pattern")
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except PermissionError:
                raise _GlobFailure("glob encountered an unreadable directory") from None
            except OSError:
                raise _GlobFailure("glob could not scan the workspace") from None

            try:
                entries.sort(key=lambda entry: entry.name.encode("utf-8"))
            except UnicodeEncodeError:
                raise _GlobFailure("glob encountered a path that is not valid UTF-8") from None

            for entry in entries:
                scanned_entries += 1
                if scanned_entries > MAX_GLOB_SCANNED_ENTRIES:
                    raise _GlobFailure("glob traversal entry limit reached; use a narrower pattern")
                name = entry.name
                try:
                    name.encode("utf-8")
                except UnicodeEncodeError:
                    raise _GlobFailure("glob encountered a path that is not valid UTF-8") from None
                candidate = relative + (name,)
                try:
                    is_file = entry.is_file(follow_symlinks=False)
                    is_directory = entry.is_dir(follow_symlinks=False)
                except PermissionError:
                    raise _GlobFailure("glob encountered an unreadable directory") from None
                except OSError:
                    raise _GlobFailure("glob could not scan the workspace") from None

                if is_file and _matches(components, candidate):
                    matches.add("/".join(candidate))
                if not is_directory or not _can_match_descendant(components, candidate):
                    continue
                child_depth = depth + 1
                if child_depth > MAX_GLOB_DEPTH:
                    raise _GlobFailure("glob depth limit reached; use a narrower pattern")
                walk(Path(entry.path), candidate, child_depth)

        walk(self._workspace, (), 0)
        return sorted(matches, key=lambda path: path.encode("utf-8"))

    @staticmethod
    def _error(request: ToolUse, content: str) -> ToolResult:
        return ToolResult(tool_use_id=request.tool_use_id, content=content, is_error=True)


def _validate_pattern(pattern: str) -> tuple[str, ...]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise _GlobFailure("glob pattern must not be blank")
    if "\x00" in pattern:
        raise _GlobFailure("glob pattern contains an unsupported path component")
    try:
        encoded = pattern.encode("utf-8")
    except UnicodeEncodeError:
        raise _GlobFailure("glob pattern contains an unsupported path component") from None
    if len(pattern) > MAX_GLOB_PATTERN_CHARACTERS or len(encoded) > MAX_GLOB_PATTERN_BYTES:
        raise _GlobFailure("glob pattern exceeds the supported size")
    if pattern.startswith("/") or "\\" in pattern or PureWindowsPath(pattern).drive:
        raise _GlobFailure("glob pattern must be workspace-relative and use '/' separators")

    components = tuple(pattern.split("/"))
    if len(components) > MAX_GLOB_PATTERN_COMPONENTS:
        raise _GlobFailure("glob pattern exceeds the supported size")
    if any(
        component in {"", ".", ".."} or ("**" in component and component != "**")
        for component in components
    ):
        raise _GlobFailure("glob pattern contains an unsupported path component")
    return components


def _matches(pattern: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
    return len(pattern) in _states_after(pattern, candidate)


def _can_match_descendant(pattern: tuple[str, ...], directory: tuple[str, ...]) -> bool:
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


def _format_matches(matches: list[str]) -> tuple[str, bool]:
    count_truncated = len(matches) > MAX_GLOB_MATCHES
    selected = matches[:MAX_GLOB_MATCHES]
    complete = "".join(f"{path}\n" for path in selected)
    if not count_truncated and len(complete.encode("utf-8")) <= MAX_GLOB_OUTPUT_BYTES:
        return complete, False

    marker_bytes = len(GLOB_TRUNCATION_MARKER.encode("utf-8"))
    output: list[str] = []
    output_bytes = 0
    for path in selected:
        line = f"{path}\n"
        line_bytes = len(line.encode("utf-8"))
        if output_bytes + line_bytes + marker_bytes > MAX_GLOB_OUTPUT_BYTES:
            break
        output.append(line)
        output_bytes += line_bytes
    output.append(GLOB_TRUNCATION_MARKER)
    return "".join(output), True
