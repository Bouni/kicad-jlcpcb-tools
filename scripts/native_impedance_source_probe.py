"""Prove source-reader reuse against a disposable real KiCad 10 fixture.

This focused probe reports acquisition, full construction and complete reader
cost separately. It retains fresh evidence acquisition for every warm read.
Run with workflow-owned native bindings under Xvfb, outside the PCB editor.
"""

import argparse
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from functools import wraps
import json
from pathlib import Path
import sys
import time
import traceback
from typing import Any, Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def measure_reads(
    read: Callable[[], Any], verify: Callable[[Any], None], *, samples: int
) -> dict[str, Any]:
    """Time every actual read and verify its result outside the measured span."""
    if type(samples) is not int or not 1 <= samples <= 1000:
        raise ValueError("The number of samples must be between 1 and 1000.")
    durations = []
    for _ in range(samples):
        started = time.perf_counter()
        value = read()
        durations.append(time.perf_counter() - started)
        verify(value)
    total = sum(durations)
    return {
        "calls": samples,
        "seconds": total,
        "mean_seconds": total / samples,
        "min_seconds": min(durations),
        "max_seconds": max(durations),
    }


def profile_acquisition_components(
    source: Any,
    adapter: Any,
    acquire: Callable[[], Any],
    verify: Callable[[Any], None],
    *,
    samples: int,
) -> dict[str, Any]:
    """Collect separate diagnostic samples after all unwrapped primary timings."""
    from scripts.native_impedance_workflow import record_call_timings

    components: dict[str, Any] = {}
    polygon_calls = 0
    native_polygons = adapter._copper_polygons

    @wraps(native_polygons)
    def polygons(*args: Any, **kwargs: Any) -> Any:
        nonlocal polygon_calls
        polygon_calls += 1
        return native_polygons(*args, **kwargs)

    targets = (
        (source, "_feature_evidence", "effective_features"),
        (adapter, "_board_settings_records", "board_settings"),
        (source, "text_context_records", "resolved_text"),
        (source, "project_context_records", "project_files"),
        (source, "netclass_context", "net_classes"),
        (source, "pairing_context", "differential_pairs"),
    )
    with (
        record_call_timings(components, targets),
        patch.object(adapter, "_copper_polygons", polygons),
    ):
        acquisition = measure_reads(acquire, verify, samples=samples)
    return {
        "diagnostic_only": True,
        "acquisition": acquisition,
        "components": components,
        "copper_polygon_calls": polygon_calls,
        "arithmetic_remainder_seconds": acquisition["seconds"]
        - sum(value["seconds"] for value in components.values()),
        "accounting": (
            "Extra diagnostic samples only; primary timings remain unwrapped. "
            "Component wall times are inclusive and may overlap. The arithmetic "
            "remainder includes native serialization, raw getter loops, writer "
            "encoding and instrumentation overhead; it is not exclusive "
            "serialization time and is not additive if components nest."
        ),
    }


def probe(output: Path, samples: int = 65) -> int:
    """Own native resources and record failures without touching source fixtures."""
    from scripts.linux_native_impedance_smoke import (
        claim_output,
        configure_palette,
        loaded_fixture,
        native_runtime,
        require,
        write_json,
    )

    if type(samples) is not int or not 1 <= samples <= 1000:
        raise ValueError("The number of samples must be between 1 and 1000.")
    output = claim_output(output)
    started = time.perf_counter()
    record: dict[str, Any] = {"status": "FAIL", "samples": samples}
    app = None
    reader = None
    try:
        configure_palette(output, False)
        pcbnew, _wx, app, versions = native_runtime()
        record["runtime"] = versions
        from impedance import pcbnew_adapter, source_evidence
        from impedance.source_evidence import (
            SourceSnapshotReader,
            acquire_source_evidence,
        )

        with loaded_fixture("usb-differential-90-ohm", output, pcbnew) as (
            _case,
            board,
            _path,
        ):
            reader = SourceSnapshotReader()
            owner_key = ("source-probe", int(board.this), str(board.GetFileName()))

            def acquire() -> Any:
                return acquire_source_evidence(board, pcbnew)

            def build() -> Any:
                return pcbnew_adapter.snapshot_board(board, pcbnew)

            def read() -> Any:
                return reader.read(acquire, build, owner_key=owner_key)

            reference = build()

            def equal_reference(value: Any) -> None:
                require(value == reference, "Native snapshot changed during probe")

            evidence = acquire()
            record["evidence"] = measure_reads(
                acquire,
                lambda value: require(value == evidence, "Source evidence is unstable"),
                samples=samples,
            )
            record["uncached_construction"] = measure_reads(
                build, equal_reference, samples=min(samples, 5)
            )
            record["cold_reader"] = measure_reads(read, equal_reference, samples=1)
            owned = read()

            def equal_owned(value: Any) -> None:
                equal_reference(value)
                require(value is owned, "Unchanged source did not reuse its snapshot")

            record["warm_reader"] = measure_reads(read, equal_owned, samples=samples)
            record["forced_reader"] = measure_reads(
                lambda: reader.read(acquire, build, owner_key=owner_key, force=True),
                equal_reference,
                samples=1,
            )
            track = next(iter(board.GetTracks()))
            width = track.GetWidth()
            try:
                track.SetWidth(width + 1)
                changed = read()
                require(changed != reference, "Raw setter left a stale snapshot")
                require(
                    changed == build(), "Changed reader differs from uncached source"
                )
            finally:
                track.SetWidth(width)
                del track
            equal_reference(read())
            stats = getattr(reader, "statistics", None)
            record["reader_statistics"] = (
                asdict(stats) if is_dataclass(stats) else stats
            )
            record["features"] = len(reference.copper_features)
            record["traces"] = len(reference.traces)
            record["fresh_checks_preserved"] = True
            record["diagnostic_components"] = profile_acquisition_components(
                source_evidence,
                pcbnew_adapter,
                acquire,
                lambda value: require(value == evidence, "Source evidence is unstable"),
                samples=min(samples, 5),
            )
            reader.close()
            reader = None
            board = None
        record["status"] = "PASS"
    except BaseException as error:
        record["error"] = str(error)
        record["traceback"] = traceback.format_exc()
        sys.stderr.write(record["traceback"] + "\n")
    finally:
        try:
            if reader is not None:
                reader.close()
            if app is not None:
                app.Destroy()
        except BaseException as error:
            record["status"] = "FAIL"
            record["cleanup_error"] = str(error)
        record["seconds"] = time.perf_counter() - started
        write_json(output / "result.json", record)
    sys.stdout.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return int(record["status"] != "PASS")


def main(argv: Optional[list[str]] = None) -> int:
    """Run only the explicit native evidence probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=65)
    args = parser.parse_args(argv)
    try:
        return probe(args.output, args.samples)
    except (OSError, ValueError) as error:
        sys.stderr.write(str(error) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
