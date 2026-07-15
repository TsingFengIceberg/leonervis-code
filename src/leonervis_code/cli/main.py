"""Command-line interface for the Foundation 0 learning slice."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from leonervis_code import __version__
from leonervis_code.agent.loop import AgentLoop
from leonervis_code.providers.fake import DeterministicFakeProvider


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
    subcommands = parser.add_subparsers(dest="command", required=True)
    prompt_parser = subcommands.add_parser("prompt", help="run one deterministic prompt turn")
    prompt_parser.add_argument("prompt", type=nonblank_prompt, help="the prompt to send")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one deterministic prompt turn through the Foundation 0 loop."""
    arguments = build_parser().parse_args(argv)
    if arguments.command == "prompt":
        response = AgentLoop(DeterministicFakeProvider()).run(arguments.prompt)
        print(response)
        return 0
    raise AssertionError(f"unhandled command: {arguments.command}")
