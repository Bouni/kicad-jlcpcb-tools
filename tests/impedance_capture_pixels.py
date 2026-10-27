"""Validate real PNGs and compare protected capture pixels in packed rows."""

from dataclasses import dataclass
import io
import math
from pathlib import Path
import struct
import sys
from typing import Optional
import zlib


@dataclass(frozen=True)
class PackedPixels:
    """Keep native decoded samples packed while permitting small pixel probes."""

    width: int
    height: int
    rgba: bytes

    def __post_init__(self) -> None:
        """Reject incomplete packed images before any row comparisons."""
        assert self.width > 0 and self.height > 0
        assert len(self.rgba) == self.width * self.height * 4

    def __len__(self) -> int:
        """Return the number of independently addressable RGBA samples."""
        return self.width * self.height

    def __getitem__(self, index: int) -> tuple[int, int, int, int]:
        """Decode one probe pixel, retaining ordinary sequence index behavior."""
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        red, green, blue, alpha = self.rgba[index * 4 : index * 4 + 4]
        return red, green, blue, alpha

    @property
    def opaque(self) -> bool:
        """Check every alpha sample using one packed byte comparison."""
        return self.rgba[3::4] == b"\xff" * len(self)

    def count(self, pixel: tuple[int, int, int, int]) -> int:
        """Count aligned pixels without treating overlapping byte strings as pixels."""
        expected = int.from_bytes(bytes(pixel), sys.byteorder)
        return sum(
            value == expected for (value,) in struct.iter_unpack("=I", self.rgba)
        )


def validate_png(data: bytes) -> tuple[int, int]:
    """Keep explicit signature, format, CRC and complete filtered-payload checks."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "Invalid PNG signature"
    offset = 8
    dimensions: Optional[tuple[int, int]] = None
    compressed = bytearray()
    channels = 0
    finished = False
    while offset < len(data):
        assert offset + 12 <= len(data), "Truncated PNG chunk"
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + length + 12
        assert end <= len(data), "Truncated PNG chunk"
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : end - 4]
        assert (
            zlib.crc32(kind + payload) & 0xFFFFFFFF
            == struct.unpack_from(">I", data, end - 4)[0]
        ), "Invalid PNG chunk CRC"
        if offset == 8:
            assert kind == b"IHDR" and length == 13, "Invalid PNG header"
            width, height, depth, color, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            assert width > 0 and height > 0, "Invalid PNG dimensions"
            assert depth == 8 and color in (2, 6) and interlace == 0, (
                "Unsupported PNG format"
            )
            assert compression == filtering == 0, (
                "Unsupported PNG compression/filtering"
            )
            dimensions = width, height
            channels = 3 if color == 2 else 4
        elif kind == b"IHDR":
            raise AssertionError("Duplicate PNG header")
        if kind == b"IDAT":
            compressed.extend(payload)
        if kind == b"IEND":
            assert not payload and end == len(data), "Invalid PNG end"
            finished = True
        offset = end
    assert dimensions is not None and finished and compressed, "Incomplete PNG"
    width, height = dimensions
    raw = zlib.decompress(compressed)
    stride = width * channels + 1
    assert len(raw) == stride * height, "Incorrect PNG decoded payload length"
    assert all(value <= 4 for value in raw[::stride]), "Invalid PNG row filter"
    return dimensions


def png_pixels(path: Path) -> tuple[int, int, PackedPixels]:
    """Use wx's native decoder after independently validating the PNG structure."""
    import wx

    data = path.read_bytes()
    width, height = validate_png(data)
    image = wx.Image(io.BytesIO(data), wx.BITMAP_TYPE_PNG)
    assert image.IsOk(), "Native PNG decoder rejected the image"
    assert (image.GetWidth(), image.GetHeight()) == (width, height)
    rgb = image.GetData()
    assert len(rgb) == width * height * 3
    alpha = image.GetAlpha() if image.HasAlpha() else b"\xff" * (width * height)
    assert len(alpha) == width * height
    rgba = bytearray(width * height * 4)
    rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = (
        rgb[0::3],
        rgb[1::3],
        rgb[2::3],
        alpha,
    )
    pixels = PackedPixels(width, height, bytes(rgba))
    return width, height, pixels


def assert_protected_pixels_match(
    actual: PackedPixels,
    baseline: PackedPixels,
    rectangle: tuple[float, float, float, float],
    *,
    border_margin: int = 3,
) -> None:
    """Compare every pixel outside the original inclusive rectangular stroke band."""
    assert (actual.width, actual.height) == (baseline.width, baseline.height)
    width = actual.width
    left, top, right, bottom = rectangle
    margin = border_margin
    for y in range(actual.height):
        allowed = []
        if min(abs(y - top), abs(y - bottom)) <= margin:
            allowed.append((left - margin, right + margin))
        if top - margin <= y <= bottom + margin:
            allowed.extend(
                ((left - margin, left + margin), (right - margin, right + margin))
            )
        intervals = sorted(
            (max(0, math.ceil(start)), min(width, math.floor(end) + 1))
            for start, end in allowed
        )
        cursor = 0
        protected = []
        for start, end in intervals:
            if end <= cursor or start >= width:
                continue
            if start > cursor:
                protected.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < width:
            protected.append((cursor, width))
        row = y * width
        for start, end in protected:
            first, last = (row + start) * 4, (row + end) * 4
            if actual.rgba[first:last] != baseline.rgba[first:last]:
                x = next(
                    x for x in range(start, end) if actual[row + x] != baseline[row + x]
                )
                raise AssertionError(
                    f"Yellow box repainted the emphasized capture at {x}, {y}"
                )
