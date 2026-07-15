"""Command-line interface for the deterministic Foundation 0 slices."""

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
from leonervis_code.providers.fake import ScriptedFakeProvider


def nonblank_prompt(value: str) -> str:
    """Reject prompt values that contain no visible characters."""
    if not value.strip():
        raise argparse.ArgumentTypeError("prompt must not be blank")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Create the small CLI surface available in Foundation 0."""
    parser = argparse.ArgumentParser(
        prog="leonervis-code",
        description="Leonervis Code: a learning-first local coding-agent CLI prototype.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command")
    prompt_parser = subcommands.add_parser("prompt", help="run one deterministic prompt turn")
    prompt_parser.add_argument("prompt", type=nonblank_prompt, help="the prompt to send")
    return parser


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
    loop = AgentLoop(ScriptedFakeProvider())
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
        cwd=cwd or Path.cwd(),
        color=color_enabled(output_stream),
    )
