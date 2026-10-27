"""Keep capture preparation shared and packed native pixel checks exact."""

import os
from pathlib import Path
import struct
import subprocess
import sys
from typing import Any
import zlib

import pytest

from tests import test_impedance_capture_examples as captures
from tests.impedance_capture_fixtures import EXAMPLES


def _chunk(kind: bytes, payload: bytes) -> bytes:
    """Create a complete independently checksummed local PNG chunk."""
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(
    width: int, height: int, channels: int, filter_type: int, samples: bytes
) -> bytes:
    """Encode known samples with every PNG predictor, independently of wx."""
    stride = width * channels
    assert len(samples) == stride * height
    previous = bytes(stride)
    rows = bytearray()
    for offset in range(0, len(samples), stride):
        row = samples[offset : offset + stride]
        rows.append(filter_type)
        for index, value in enumerate(row):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            else:
                estimate = left + above - upper_left
                distances = tuple(
                    abs(estimate - candidate) for candidate in (left, above, upper_left)
                )
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            rows.append((value - predictor) & 255)
        previous = row
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(
            b"IHDR",
            struct.pack(
                ">IIBBBBB", width, height, 8, 2 if channels == 3 else 6, 0, 0, 0
            ),
        )
        + _chunk(b"IDAT", zlib.compress(bytes(rows)))
        + _chunk(b"IEND", b"")
    )


def test_native_svg_assertions_consume_the_fresh_shared_gallery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SVG checks must consume the same real plot already used by PNG checks."""
    example = EXAMPLES[0]
    source = captures._native_fixture_svg(example.traces(), "#167db4")
    source = source.replace("</svg>", '<path d="M0,0 L1,1"/>' * 24 + "</svg>")
    (tmp_path / f"{example.name}-native.svg").write_text(source, encoding="utf-8")
    monkeypatch.setenv("KICAD_CLI", "test-owned-kicad-cli")

    def redundant_plot(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("SVG assertions repeated an existing native plot")

    monkeypatch.setattr(captures, "export_example_svg", redundant_plot, raising=False)
    captures.test_real_kicad_cli_plots_feed_production_capture(tmp_path, example)


@pytest.mark.parametrize("rasterize", (False, True))
def test_shared_gallery_runs_the_real_generator_once_for_both_assertion_groups(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    rasterize: bool,
) -> None:
    """Keep fresh invocation inputs and share output without generating a second plot."""
    monkeypatch.setenv("KICAD_CLI", "/test-owned/bin/kicad-cli")
    monkeypatch.setenv("KICAD_CAPTURE_PNG", "1" if rasterize else "0")
    calls = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record one actual gallery command instead of invoking a host KiCad."""
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(captures.subprocess, "run", run)
    gallery = captures.native_plot_gallery.__wrapped__(tmp_path_factory)
    assert len(calls) == 1
    command, options = calls[0]
    assert command[:2] == [
        sys.executable,
        str(
            Path(captures.__file__).resolve().parents[1]
            / "scripts/generate_impedance_captures.py"
        ),
    ]
    assert command[2:6] == [
        "--output",
        str(gallery),
        "--kicad-cli",
        "/test-owned/bin/kicad-cli",
    ]
    assert command[6:] == ([] if rasterize else ["--svg-only"])
    assert options == {
        "check": True,
        "capture_output": True,
        "text": True,
        "timeout": 120,
    }
    if rasterize:
        assert captures.native_capture_gallery.__wrapped__(gallery) == gallery
    else:
        with pytest.raises(pytest.skip.Exception):
            captures.native_capture_gallery.__wrapped__(gallery)
    assert len(calls) == 1


@pytest.mark.parametrize("channels", (3, 4))
@pytest.mark.parametrize("filter_type", range(5))
def test_png_validation_keeps_all_filters_and_exact_chunk_checks(
    channels: int, filter_type: int
) -> None:
    """Validate the complete compressed payload before delegating decode to wx."""
    from tests.impedance_capture_pixels import validate_png

    samples = bytes(
        (index * 71 + index // 7) % 256 for index in range(7 * 5 * channels)
    )
    data = _png(7, 5, channels, filter_type, samples)
    assert validate_png(data) == (7, 5)
    changed = bytearray(data)
    changed[-5] ^= 1
    with pytest.raises(AssertionError, match="CRC"):
        validate_png(bytes(changed))


@pytest.mark.parametrize(
    ("damage", "message"),
    (
        ("signature", "signature"),
        ("truncated", "Truncated"),
        ("depth", "format"),
        ("color", "format"),
        ("interlace", "format"),
        ("compression", "compression"),
        ("filtering", "filtering"),
        ("dimensions", "dimensions"),
        ("filter", "row filter"),
        ("length", "payload length"),
        ("missing_end", "Incomplete"),
        ("extra_data", "end"),
    ),
)
def test_png_validation_rejects_invalid_structure_even_with_valid_crcs(
    damage: str, message: str
) -> None:
    """Do not let a native decoder's leniency hide corrupt source captures."""
    from tests.impedance_capture_pixels import validate_png

    header = bytearray(struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
    raw = b"\0" + bytes(8) + b"\0" + bytes(8)
    header_changes = {
        "depth": (8, 16),
        "color": (9, 0),
        "interlace": (12, 1),
        "compression": (10, 1),
        "filtering": (11, 1),
        "dimensions": (3, 0),
    }
    if damage in header_changes:
        index, value = header_changes[damage]
        header[index] = value
    if damage == "filter":
        raw = b"\5" + raw[1:]
    if damage == "length":
        raw = raw[:-1]
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", bytes(header))
        + _chunk(b"IDAT", zlib.compress(raw))
    )
    if damage != "missing_end":
        data += _chunk(b"IEND", b"")
    if damage == "signature":
        data = b"!" + data[1:]
    if damage == "truncated":
        data = data[:-1]
    if damage == "extra_data":
        data += b"unexpected"
    with pytest.raises(AssertionError, match=message):
        validate_png(data)


@pytest.mark.parametrize("channel", range(4))
@pytest.mark.parametrize(
    "rectangle",
    ((4.25, 3.75, 13.5, 11.25), (-2.75, -1.25, 9.5, 8.75), (8, 7, 25, 20)),
)
def test_packed_comparison_protects_every_original_pixel(
    rectangle: tuple[float, float, float, float],
    channel: int,
) -> None:
    """Check each possible changed pixel against the original geometric predicate."""
    from tests.impedance_capture_pixels import (
        PackedPixels,
        assert_protected_pixels_match,
    )

    width, height = 19, 16
    baseline = PackedPixels(width, height, bytes((12, 34, 56, 255)) * width * height)
    left, top, right, bottom = rectangle
    for y in range(height):
        for x in range(width):
            changed = bytearray(baseline.rgba)
            changed[(y * width + x) * 4 + channel] ^= 1
            actual = PackedPixels(width, height, bytes(changed))
            horizontal = (
                left - 3 <= x <= right + 3 and min(abs(y - top), abs(y - bottom)) <= 3
            )
            vertical = (
                top - 3 <= y <= bottom + 3 and min(abs(x - left), abs(x - right)) <= 3
            )
            if horizontal or vertical:
                assert_protected_pixels_match(actual, baseline, rectangle)
            else:
                with pytest.raises(AssertionError, match=f"at {x}, {y}"):
                    assert_protected_pixels_match(actual, baseline, rectangle)


@pytest.mark.native_kicad
@pytest.mark.kicad10_impedance
def test_real_wx_png_decoder_matches_every_known_sample_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    """Retain a genuine native decoder proof for RGB/RGBA and all row filters."""
    if os.environ.get("KICAD_CAPTURE_PNG") != "1":
        pytest.skip("Set KICAD_CAPTURE_PNG=1 for real native PNG decoder checks")
    from tests.impedance_capture_pixels import png_pixels

    for channels in (3, 4):
        samples = bytearray(
            (index * 71 + index // 7) % 256 for index in range(7 * 5 * channels)
        )
        if channels == 4:
            samples[:8] = bytes((17, 33, 64, 0, 200, 17, 69, 255))
        source_samples = bytes(samples)
        expected = [
            tuple(source_samples[index : index + channels])
            + ((255,) if channels == 3 else ())
            for index in range(0, len(source_samples), channels)
        ]
        for filter_type in range(5):
            path = tmp_path / f"{channels}-{filter_type}.png"
            data = _png(7, 5, channels, filter_type, source_samples)
            path.write_bytes(data)
            width, height, actual = png_pixels(path)
            assert (width, height) == (7, 5)
            assert [actual[index] for index in range(len(actual))] == expected
            assert actual.opaque == all(pixel[3] == 255 for pixel in expected)
            for pixel in set(expected):
                assert actual.count(pixel) == expected.count(pixel)
            changed = bytearray(data)
            changed[-5] ^= 1
            path.write_bytes(changed)
            with pytest.raises(AssertionError, match="CRC"):
                png_pixels(path)
