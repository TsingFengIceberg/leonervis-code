#!/usr/bin/env python3
"""Render a temporary ANSI-color Leonervis Code startup-banner sample."""

from __future__ import annotations

import argparse
import os

RESET = "\x1b[0m"
BOLD_WHITE = "\x1b[1;97m"
DIM = "\x1b[2m"

# The three equal 5×5 modules agreed for the temporary LEO mark.
L_GLYPH = ("█    ", "█    ", "█    ", "█    ", "█████")
E_GLYPH = ("█████", "█    ", "█████", "█    ", "█████")
O_GLYPH = (" ███ ", "█   █", "█   █", "█   █", " ███ ")

# Tail → body → head: dark amber → gold → light gold.
TAIL = (166, 90, 24)
BODY = (230, 154, 43)
HEAD = (255, 224, 154)


def rgb(red: int, green: int, blue: int) -> str:
    """Return an ANSI truecolor foreground escape sequence."""
    return f"\x1b[38;2;{red};{green};{blue}m"


def paint(text: str, color: tuple[int, int, int], *, enabled: bool) -> str:
    """Apply a foreground color only when color output is enabled."""
    if not enabled:
        return text
    return f"{rgb(*color)}{text}{RESET}"


def build_mark(*, color: bool) -> list[str]:
    """Render LEO as joined L/E modules and a one-cell gap before O."""
    return [
        f"{paint(L_GLYPH[row], TAIL, enabled=color)}"
        f"{paint(E_GLYPH[row], BODY, enabled=color)} "
        f"{paint(O_GLYPH[row], HEAD, enabled=color)}"
        for row in range(5)
    ]


def main() -> int:
    """Print a sample banner; it is not part of the Leonervis CLI yet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="render a monochrome fallback instead of ANSI truecolor",
    )
    arguments = parser.parse_args()
    color = not arguments.no_color and not os.environ.get("NO_COLOR")

    mark = build_mark(color=color)
    title = f"{BOLD_WHITE}LEONERVIS CODE{RESET}" if color else "LEONERVIS CODE"
    details = [
        f"{title} v0.1.0",
        "sample startup banner · color study",
        "/root/Projects/leonervis-code",
        "temporary preview — not part of the CLI",
    ]

    print()
    for row, icon_row in enumerate(mark):
        suffix = details[row] if row < len(details) else ""
        if color and row > 0:
            suffix = f"{DIM}{suffix}{RESET}"
        print(f"  {icon_row}    {suffix}".rstrip())
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
