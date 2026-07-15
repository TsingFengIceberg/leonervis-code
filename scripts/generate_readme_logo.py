#!/usr/bin/env python3
"""Generate the transparent README PNG for the Leonervis LEO mark."""

from __future__ import annotations

from pathlib import Path
import struct
import zlib

from leonervis_code.cli.brand import BODY, E_GLYPH, HEAD, L_GLYPH, O_GLYPH, TAIL

OUTPUT_PATH = Path(__file__).parents[1] / "docs" / "assets" / "leo-mark.png"
CELL_SIZE = 28
PADDING = 20


def chunk(kind: bytes, data: bytes) -> bytes:
    """Encode one PNG chunk."""
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def mark_rows() -> list[list[tuple[int, int, int] | None]]:
    """Return the colored LEO pixel grid from the terminal-banner source."""
    rows: list[list[tuple[int, int, int] | None]] = []
    for row in range(len(L_GLYPH)):
        cells: list[tuple[int, int, int] | None] = []
        for glyph, color in (
            (L_GLYPH[row], TAIL),
            (E_GLYPH[row], BODY),
            (" ", None),
            (O_GLYPH[row], HEAD),
        ):
            cells.extend(color if character != " " else None for character in glyph)
        rows.append(cells)
    return rows


def png_bytes() -> bytes:
    """Build a transparent RGBA PNG containing the scaled LEO mark."""
    cells = mark_rows()
    grid_height = len(cells)
    grid_width = len(cells[0])
    width = grid_width * CELL_SIZE + PADDING * 2
    height = grid_height * CELL_SIZE + PADDING * 2

    transparent = bytes((0, 0, 0, 0))
    scanlines = bytearray()
    for pixel_row in range(height):
        scanlines.append(0)
        for pixel_column in range(width):
            grid_row = (pixel_row - PADDING) // CELL_SIZE
            grid_column = (pixel_column - PADDING) // CELL_SIZE
            color = (
                cells[grid_row][grid_column]
                if PADDING <= pixel_row < height - PADDING
                and PADDING <= pixel_column < width - PADDING
                else None
            )
            scanlines.extend((*color, 255) if color else transparent)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    """Write the deterministic transparent logo asset."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(png_bytes())
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
