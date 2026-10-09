"""Write small RGB PNGs with the standard library, for visual-evidence tests."""

import struct
import zlib
from pathlib import Path

COLOURS = {
    "red": (220, 30, 30),
    "blue": (30, 60, 220),
    "green": (30, 170, 60),
    "yellow": (240, 210, 20),
}
QUADRANTS = ("top_left", "top_right", "bottom_left", "bottom_right")


def chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def png_bytes(width: int, height: int, pixel) -> bytes:
    rows = b"".join(
        b"\x00" + bytes(c for x in range(width) for c in pixel(x, y)) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def write_quadrants(path: Path, colours: dict, size: int = 128) -> Path:
    """A ``size``-square PNG whose four quadrants have the named colours."""

    half = size // 2

    def pixel(x, y):
        return COLOURS[colours[QUADRANTS[(y >= half) * 2 + (x >= half)]]]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(size, size, pixel))
    return path
