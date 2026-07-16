"""Command-line interface for the deterministic Foundation 1B slices."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from leonervis_code import __version__
from leonervis_code.agent.loop import AgentLoop
from leonervis_code.cli.brand import color_enabled
from leonervis_code.cli.repl import run_repl
from leonervis_code.core.contracts import AssistantText, ToolResult, ToolUse
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.tools.read_file import ReadFileTool


def nonblank_prompt(value: str) -> str:
    """Reject prompt values that contain no visible characters."""
    if not value.strip():
        raise argparse.ArgumentTypeError("prompt must not be blank")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Create the small CLI surface available in Foundation 1B."""
    parser = argparse.ArgumentParser(
        prog="leonervis-code",
        description="Leonervis Code: a learning-first local coding-agent CLI prototype.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command")
    prompt_parser = subcommands.add_parser("prompt", help="run one deterministic prompt turn")
    prompt_parser.add_argument("prompt", type=nonblank_prompt, help="the prompt to send")
    demo_read_parser = subcommands.add_parser(
        "demo-read", help="visibly demonstrate one deterministic read_file tool loop"
    )
    demo_read_parser.add_argument("path", help="relative workspace path for the demonstration")
    return parser


def render_demo_read(workspace: Path, path: str, stdout: TextIO) -> int:
    """Run and visibly report one scripted ``read_file`` tool demonstration."""
    tool_use = ToolUse(tool_use_id="demo-read-1", name="read_file", path=path)
    provider = ScriptedFakeProvider(
        [
            tool_use,
            AssistantText(text="Demo final response: provider received the read_file result."),
        ]
    )
    demo_loop = AgentLoop(provider, ReadFileTool(workspace))
    stdout.write(f"[demo] provider requested read_file: {path}\n")
    response = demo_loop.run(f"Demo read {path}")
    result = provider.received_histories[1][-1]
    assert isinstance(result, ToolResult)
    if result.is_error:
        stdout.write(f"[read_file] {path}\n  ✗ {result.content}\n")
    else:
        truncation = " (truncated)" if result.truncated else ""
        preview = result.content.splitlines()[0] if result.content else "<empty file>"
        stdout.write(
            f"[read_file] {path}\n"
            f"  ✓ {len(result.content.encode('utf-8'))} UTF-8 bytes returned{truncation}\n"
            f"  preview: {preview}\n"
        )
    stdout.write(f"{response}\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    cwd: Path | None = None,
) -> int:
    """Run a one-shot prompt command or launch the interactive terminal surface."""
    arguments = build_parser().parse_args(argv)
    workspace = cwd or Path.cwd()
    if arguments.command == "demo-read":
        return render_demo_read(workspace, arguments.path, stdout or sys.stdout)

    loop = AgentLoop(ScriptedFakeProvider(), ReadFileTool(workspace))
    if arguments.command == "prompt":
        print(loop.run(arguments.prompt))
        return 0

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    if not input_stream.isatty() or not output_stream.isatty():
        print(
            'interactive mode requires a terminal; use leonervis-code prompt "..." instead',
            file=error_stream,
        )
        return 2
    return run_repl(
        loop,
        stdin=input_stream,
        stdout=output_stream,
        version=__version__,
        cwd=workspace,
        color=color_enabled(output_stream),
    )
