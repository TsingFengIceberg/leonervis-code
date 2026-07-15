"""Command-line bootstrap for Leonervis Code."""

from __future__ import annotations

import argparse

from leonervis_code import __version__


def build_parser() -> argparse.ArgumentParser:
    """Create the small CLI surface available before the Harness runtime exists."""
    parser = argparse.ArgumentParser(
        prog="leonervis-code",
        description="Leonervis Code: a learning-first local coding-agent CLI prototype.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main() -> int:
    """Run the bootstrap CLI and report the intentionally unimplemented runtime."""
    parser = build_parser()
    parser.parse_args()
    parser.error("the Harness runtime has not been implemented yet")
    return 2
