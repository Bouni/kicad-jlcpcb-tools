r"""Fabrication outputs of the fixture boards match their committed expected files.

Each board in tests/golden/ goes through the plugin's generation steps, in the
order the main window runs them, on a temporary copy: zone refill, gerbers,
drill and map files, the gerber archive, then the CPL and the BOM. Every file
the plugin publishes is compared with tests/golden/<board>/<KiCad major>/,
where a zip archive is a directory of its members. Only creation dates and the
KiCad build in file headers are normalised (``normalise``), and the expected
files are stored normalised.

The expected files are captured from the Ubuntu builds that tests.yml installs
from the KiCad release PPAs, and tests/golden/kicad_builds.json records which
build each major's files came from. On that build any difference fails.

On any other build of the major, such as a newer PPA release or KiCad on macOS,
only the files KiCad plots may differ: a changed gerber, drill file or drill
map warns instead of failing. The warning names the build and the changed
files, shows the first diffs, and says how to regenerate on the recorded build
or move the expected files to this one. The drill map PDFs, whose text other
builds draw differently (KiCad 10.0.4 on macOS against 10.0.7 on Ubuntu), are
compared by name only there. The BOM and CPL are the plugin's own output, so a
change to either fails on every build, as do a board that cannot be generated
and a missing or extra file.

After an intended change to the output, regenerate the expected files on the
recorded build of each major with KICAD_JLCPCB_GOLDEN_UPDATE=1, then review and
commit the changes:

    KICAD_JLCPCB_GOLDEN_UPDATE=1 python -m pytest -p no:cacheprovider \
        --require-native=kicad --kicad-major=10 -m native_kicad \
        tests/test_golden_outputs.py

On any other build that command refuses, so expected files never move away
from the build CI installs by accident. When CI moves to a new build, set
KICAD_JLCPCB_GOLDEN_UPDATE to that build's exact pcbnew.GetBuildVersion()
string instead of 1 and run the command on that build; it rewrites the
expected files and records the new build.

The boards themselves are written by tests/golden/make_boards.py.
"""

from collections.abc import Iterator, Mapping
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
import warnings
from zipfile import ZipFile
import zlib

import pytest

from tests.native_kicad_support import native_bindings, requires_native_plotting
from tests.wx_harness import load_siblings

__all__ = ["native_bindings"]

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"


def update_request(environ: Mapping[str, str]) -> str:
    """Read KICAD_JLCPCB_GOLDEN_UPDATE: "" compares, anything else asks to rewrite.

    "1" rewrites the expected files on the recorded build, and an exact build
    string moves them to that build. Unset, empty or "0" compares.
    """
    request = environ.get("KICAD_JLCPCB_GOLDEN_UPDATE", "").strip()
    return "" if request == "0" else request


UPDATE = update_request(os.environ)
# Files KiCad's plotter writes, which another build of a major may draw differently.
_PLOTTED = frozenset({".gbr", ".drl", ".pdf"})


class GoldenDifferenceWarning(UserWarning):
    """KiCad plotted differently on a build the expected files did not come from."""


def regenerate_command(major: str, build: str = "1") -> str:
    """Return the one-line command that rewrites one major's expected files.

    ``build`` is "1" on the recorded build, or the exact build to move them to.
    """
    return (
        f"KICAD_JLCPCB_GOLDEN_UPDATE={shlex.quote(build)} python -m pytest "
        f"-p no:cacheprovider --require-native=kicad --kicad-major={major} "
        "-m native_kicad tests/test_golden_outputs.py"
    )


def regeneration_advice(major: str, build: str, captured: Optional[str]) -> list[str]:  # noqa: UP045
    """Say on which build to regenerate the expected files, and with which command."""
    if captured in (None, build):
        return [
            "To accept these outputs, regenerate the expected files and review them:",
            regenerate_command(major),
        ]
    return [
        f"To accept these outputs, regenerate the expected files on KiCad {captured}, "
        "the build CI installs:",
        regenerate_command(major),
        f"If CI now installs KiCad {build} instead, move the expected files to it "
        "from this build:",
        regenerate_command(major, build),
    ]


# Header lines that carry the creation time, with its time zone, or the KiCad
# build. Each named group is replaced by its name in angle brackets.
_HEADERS = {
    ".gbr": (
        rb"^G04 Created by KiCad \(PCBNEW (?P<build>.+)\) date (?P<date>.+)\*$",
        rb"^%TF\.GenerationSoftware,KiCad,Pcbnew,(?P<build>.+)\*%$",
        rb"^%TF\.CreationDate,(?P<date>.+)\*%$",
    ),
    ".drl": (
        rb"^; DRILL file \{?KiCad (?P<build>.+?)\}? date (?P<date>.+)$",
        rb"^; #@! TF\.CreationDate,(?P<date>.+)$",
        rb"^; #@! TF\.GenerationSoftware,Kicad,Pcbnew,(?P<build>.+)$",
    ),
    ".pdf": (rb"^/CreationDate \(D:(?P<date>[^)]+)\)$",),
}
_HEADER_PATTERNS = {
    suffix: tuple(re.compile(pattern, re.MULTILINE) for pattern in patterns)
    for suffix, patterns in _HEADERS.items()
}


def _normalise_line(match: re.Match[bytes]) -> bytes:
    """Replace a matched line's creation date and KiCad build with placeholders."""
    line = match.group(0)
    # Replace from the end of the line so earlier offsets stay valid.
    for name in sorted(match.re.groupindex, key=match.start, reverse=True):
        begin, end = match.start(name) - match.start(), match.end(name) - match.start()
        line = line[:begin] + f"<{name}>".encode() + line[end:]
    return line


def normalise(name: str, data: bytes) -> bytes:
    """Remove what changes from run to run: creation dates and the KiCad build."""
    for pattern in _HEADER_PATTERNS.get(Path(name).suffix, ()):
        data = pattern.sub(_normalise_line, data)
    return data


def published_files(outputdir: Path) -> dict[str, bytes]:
    """Read every published artifact, with each archive's members by name."""
    files = {}
    for path in sorted(outputdir.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix == ".zip":
            with ZipFile(path) as archive:
                for member in archive.namelist():
                    files[f"{path.stem}/{member}"] = normalise(
                        member, archive.read(member)
                    )
        else:
            files[path.name] = normalise(path.name, path.read_bytes())
    return files


def expected_files(directory: Path) -> dict[str, bytes]:
    """Read the committed expected files, keyed as ``published_files`` keys them."""
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _inflated(match: re.Match[bytes]) -> bytes:
    """Show a compressed PDF stream's drawing commands."""
    try:
        return b"stream\n" + zlib.decompress(match.group(1)) + b"\nendstream"
    except zlib.error:
        return match.group(0)


def _lines(name: str, data: bytes) -> list[str]:
    """Split a file into lines for a diff, with PDF streams decompressed."""
    if name.endswith(".pdf"):
        data = re.sub(rb"(?s)stream\r?\n(.*?)\r?\nendstream", _inflated, data)
    return data.decode("utf-8", errors="replace").splitlines()


def describe_difference(
    label: str, expected: dict[str, bytes], actual: dict[str, bytes]
) -> str:
    """Name each differing file and show the start of the first diffs."""
    lines = [f"{label} differs from its expected files:"]
    lines += [f"  missing: {name}" for name in sorted(expected.keys() - actual.keys())]
    lines += [
        f"  unexpected: {name}" for name in sorted(actual.keys() - expected.keys())
    ]
    changed = sorted(
        name
        for name in expected.keys() & actual.keys()
        if expected[name] != actual[name]
    )
    lines += [f"  changed: {name}" for name in changed]
    for name in changed[:3]:
        diff = list(
            difflib.unified_diff(
                _lines(name, expected[name]),
                _lines(name, actual[name]),
                f"expected/{name}",
                f"actual/{name}",
                n=1,
                lineterm="",
            )
        )
        lines += diff[:24]
        if len(diff) > 24:
            lines.append(f"... {len(diff) - 24} more diff lines")
    return "\n".join(lines)


def _captured_builds(golden: Path) -> dict[str, str]:
    """Read which KiCad build captured each major's expected files."""
    builds = golden / "kicad_builds.json"
    return json.loads(builds.read_text("utf-8")) if builds.is_file() else {}


def _record_build(golden: Path, major: str, build: str) -> None:
    """Note the build that just rewrote a major's expected files."""
    builds = {**_captured_builds(golden), major: build}
    (golden / "kicad_builds.json").write_text(
        json.dumps(builds, indent=2, sort_keys=True) + "\n", "utf-8"
    )


def _without_pdf_contents(files: dict[str, bytes]) -> dict[str, bytes]:
    """Keep each drill map PDF's name but not what it draws."""
    return {
        name: b"" if name.endswith(".pdf") else data for name, data in files.items()
    }


def _check_update_request(
    request: str,
    major: str,
    build: str,
    captured: Optional[str],  # noqa: UP045
) -> None:
    """Refuse to move expected files off the recorded build unless asked by name."""
    if request == build or (request == "1" and captured in (None, build)):
        return
    if request == "1":
        reason = (
            f"KiCad {build} is not KiCad {captured}, the build the expected files "
            "come from and CI installs, so KICAD_JLCPCB_GOLDEN_UPDATE=1 would move "
            "them off that build."
        )
    else:
        reason = (
            f"KICAD_JLCPCB_GOLDEN_UPDATE names KiCad {request}, but this is KiCad "
            f"{build}."
        )
    pytest.fail(
        "\n".join([reason, *regeneration_advice(major, build, captured)]),
        pytrace=False,
    )


def check_golden(
    board: str,
    pcbnew: ModuleType,
    actual: dict[str, bytes],
    golden: Path = GOLDEN,
    update: str = UPDATE,
) -> None:
    """Compare published files with the expected ones, or rewrite them on request.

    On the build recorded for the major any difference fails. On any other
    build a changed plot only warns, while a changed BOM or CPL, or a missing
    or extra file, still fails. Rewriting happens only on the recorded build,
    or on the build the request names, which then becomes the recorded one.
    """
    build = str(pcbnew.GetBuildVersion())
    major = str(pcbnew.GetMajorMinorVersion()).split(".")[0]
    directory = golden / board / major
    captured = _captured_builds(golden).get(major)
    if update:
        _check_update_request(update, major, build, captured)
        shutil.rmtree(directory, ignore_errors=True)
        for name, data in actual.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _record_build(golden, major, build)
        captured = build
    expected = expected_files(directory)
    label = f"{board} on KiCad {build}"
    advice = regeneration_advice(major, build, captured)
    if not expected:
        pytest.fail(
            "\n".join([f"{label} has no expected files in {directory}.", *advice]),
            pytrace=False,
        )
    recorded = captured in (None, build)
    if not recorded:
        warnings.warn(
            f"{label}: drill map PDFs compared by name only; their expected "
            f"contents come from KiCad {captured}",
            stacklevel=2,
        )
        expected, actual = (
            _without_pdf_contents(expected),
            _without_pdf_contents(actual),
        )
    if expected == actual:
        return
    message = "\n".join([describe_difference(label, expected, actual), *advice])
    plots_only = expected.keys() == actual.keys() and all(
        Path(name).suffix in _PLOTTED
        for name in expected
        if expected[name] != actual[name]
    )
    if recorded or not plots_only:
        pytest.fail(message, pytrace=False)
    warnings.warn(
        GoldenDifferenceWarning(
            f"Only a warning: this is KiCad {build}, and the expected files come "
            f"from KiCad {captured}, the build CI installs.\n" + message
        ),
        stacklevel=2,
    )


def _default_gerber_settings() -> dict[str, Any]:
    """Use the generation settings a new installation starts with."""
    defaults = json.loads((ROOT / "default_settings.json").read_text("utf-8"))
    return {"gerber": defaults["gerber"]}


def _corrections(correction_data: ModuleType) -> tuple[Any, ...]:
    """Turn and shift some parts by package and by part number, as users do."""
    return (
        correction_data.Correction("^SOT-23$", 180, (0, 0)),
        correction_data.Correction("^PinHeader_1x0[23]_", 270, (0.25, -0.5)),
        correction_data.LcscCorrection("C7950", 90, (0.5, 0)),
    )


def _copy_board(name: str, directory: Path) -> Path:
    """Copy a fixture board into its own project folder under the test's tmp."""
    project = directory / name
    project.mkdir()
    return Path(shutil.copy(GOLDEN / name / f"{name}.kicad_pcb", project))


@pytest.fixture
def golden_modules(native_bindings: SimpleNamespace) -> Iterator[SimpleNamespace]:
    """Load the plugin's generation modules against the real KiCad bindings."""
    with load_siblings(
        "_golden_outputs_test",
        (
            "fabrication",
            "store",
            "correction_data",
            "variant.native",
            "variant.store",
            "variant.session",
            "core.settings_persistence",
        ),
        {},
    ) as modules:
        yield SimpleNamespace(
            pcbnew=native_bindings.pcbnew,
            **{name.replace(".", "_"): module for name, module in modules.items()},
        )


def export_ordinary(modules: SimpleNamespace, board_path: Path) -> Path:
    """Generate a board's files as the main window does without design variants."""
    board = modules.pcbnew.LoadBoard(str(board_path))
    parent = SimpleNamespace(settings=_default_gerber_settings())
    parent.store = modules.store.Store(parent, str(board_path.parent), board)
    fabrication = modules.fabrication.Fabrication(parent, board)
    output = fabrication.begin_ordinary_generation(
        _corrections(modules.correction_data)
    )
    try:
        assert fabrication.get_part_consistency_warnings() == {}
        assert fabrication.fill_zones() == []
        fabrication.validate_generation()
        fabrication.generate_geber(None)
        fabrication.generate_excellon()
        fabrication.zip_gerber_excellon()
        fabrication.write_cpl(output.cpl_rows)
        fabrication.generate_bom()
        fabrication.validate_generation()
    finally:
        fabrication.end_ordinary_generation()
    return Path(fabrication.outputdir)


def export_variants(
    modules: SimpleNamespace, board_path: Path, names: tuple[str, ...]
) -> Path:
    """Generate each named variant's files as the main window's variant mode does."""
    board = modules.pcbnew.LoadBoard(str(board_path))
    parent = SimpleNamespace(settings=_default_gerber_settings())

    def save_settings(variant_patch: Any = None) -> None:
        saved = modules.core_settings_persistence.save_settings_document(
            board_path.parent, parent.settings, variant_patch
        )
        parent.settings.clear()
        parent.settings.update(saved)

    parent.save_settings = save_settings
    cache = modules.variant_store.VariantStore(parent, str(board_path.parent), board)
    adapter = modules.variant_native.VariantNativeAdapter(board, cache.board_id)
    session = modules.variant_session.VariantSession(
        adapter, cache, get_board=lambda: board
    )
    fabrication = modules.fabrication.Fabrication(parent, board)
    corrections = _corrections(modules.correction_data)
    for name in names:
        session.set_output_variant(name)
        snapshot, output = session.begin_generation()
        try:
            fabrication.begin_generation(
                snapshot, output, corrections, session.validate_generation
            )
            placements = fabrication.prepare_cpl(corrections)
            assert fabrication.get_part_consistency_warnings() == {}
            assert fabrication.fill_zones() == []
            fabrication.validate_generation()
            fabrication.generate_geber(None)
            fabrication.generate_excellon()
            fabrication.zip_gerber_excellon()
            fabrication.write_cpl(placements)
            fabrication.generate_bom()
            fabrication.publish_generation()
        finally:
            fabrication.abort_generation()
            session.end_generation()
    return Path(fabrication.outputdir)


@pytest.mark.native_kicad
@requires_native_plotting
@pytest.mark.parametrize("board", ["two_layer", "four_layer"])
def test_board_outputs_match_their_expected_files(
    golden_modules: SimpleNamespace, tmp_path: Path, board: str
) -> None:
    """BOM, CPL, gerbers and drill files of a board without variants."""
    outputdir = export_ordinary(golden_modules, _copy_board(board, tmp_path))
    check_golden(board, golden_modules.pcbnew, published_files(outputdir))


@pytest.mark.native_kicad
@pytest.mark.kicad_variants
@requires_native_plotting
def test_variant_outputs_match_their_expected_files(
    golden_modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Default and each design variant publish their own complete set of files."""
    outputdir = export_variants(
        golden_modules, _copy_board("variants", tmp_path), ("", "Lite", "Full")
    )
    check_golden("variants", golden_modules.pcbnew, published_files(outputdir))


@pytest.mark.native_kicad
@requires_native_plotting
def test_repeated_references_are_refused_before_any_output(
    golden_modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Two footprints named R1 stop generation before a file is written."""
    board_path = _copy_board("repeated_references", tmp_path)
    with pytest.raises(ValueError, match="duplicate component references"):
        export_ordinary(golden_modules, board_path)
    outputs = board_path.parent / "jlcpcb"
    assert [path for path in outputs.rglob("*") if path.is_file()] == []


@pytest.mark.parametrize(
    ("name", "header", "normalised"),
    [
        (
            "board-CuTop.gbr",
            b"G04 Created by KiCad (PCBNEW 9.0.9-9.0.9~ubuntu24.04.1) date 2026-10-12 16:28:45*",
            b"G04 Created by KiCad (PCBNEW <build>) date <date>*",
        ),
        (
            "board-CuTop.gbr",
            b"%TF.GenerationSoftware,KiCad,Pcbnew,10.0.4*%",
            b"%TF.GenerationSoftware,KiCad,Pcbnew,<build>*%",
        ),
        (
            "board-CuTop.gbr",
            b"%TF.CreationDate,2026-10-12T09:36:01-07:00*%",
            b"%TF.CreationDate,<date>*%",
        ),
        (
            "board-PTH.drl",
            b"; DRILL file {KiCad 9.0.9-9.0.9~ubuntu24.04.1} date 2026-10-12T16:28:45+0000",
            b"; DRILL file {KiCad <build>} date <date>",
        ),
        (
            "board-PTH.drl",
            b"; DRILL file KiCad 10.0.4 date 2026-10-12T09:31:06",
            b"; DRILL file KiCad <build> date <date>",
        ),
        (
            "board-PTH.drl",
            b"; #@! TF.CreationDate,2026-10-12T16:29:38+00:00",
            b"; #@! TF.CreationDate,<date>",
        ),
        (
            "board-PTH.drl",
            b"; #@! TF.GenerationSoftware,Kicad,Pcbnew,10.0.7-10.0.7~ubuntu24.04.1",
            b"; #@! TF.GenerationSoftware,Kicad,Pcbnew,<build>",
        ),
        (
            "board-PTH-drl_map.pdf",
            b"/CreationDate (D:2026:10:12:16:29:38)",
            b"/CreationDate (D:<date>)",
        ),
        (
            "CPL-board.csv",
            b"Created 2026-10-12 by KiCad 10.0.7",
            b"Created 2026-10-12 by KiCad 10.0.7",
        ),
    ],
)
def test_only_creation_dates_and_kicad_builds_are_normalised(
    name: str, header: bytes, normalised: bytes
) -> None:
    """The lines around a header, and every other file type, stay byte for byte."""
    around = b"%TF.ProjectId,board,626f6172-642e-4b69,rev?*%\nX1000000Y-2000000D03*\n"
    assert normalise(name, around + header + b"\n" + around) == (
        around + normalised + b"\n" + around
    )


def test_a_difference_names_each_file_and_shows_the_changed_lines() -> None:
    """A failure lists missing, unexpected and changed files, then their diffs."""
    expected = {
        "CPL-board.csv": b"Designator,Rotation\r\nR1,0.0\r\n",
        "GERBER-board/board-CuTop.gbr": b"%FSLAX46Y46*%\n",
    }
    actual = {
        "CPL-board.csv": b"Designator,Rotation\r\nR1,90.0\r\n",
        "GERBER-board/board-PasteTop.gbr": b"%FSLAX46Y46*%\n",
    }

    lines = describe_difference("board on KiCad 10.0.7", expected, actual).splitlines()

    assert lines[:4] == [
        "board on KiCad 10.0.7 differs from its expected files:",
        "  missing: GERBER-board/board-CuTop.gbr",
        "  unexpected: GERBER-board/board-PasteTop.gbr",
        "  changed: CPL-board.csv",
    ]
    assert lines[-2:] == ["-R1,0.0", "+R1,90.0"]


def test_a_drill_map_difference_shows_its_drawing_commands() -> None:
    """Compressed PDF streams are shown decompressed, so the diff is readable."""

    def drill_map(command: bytes) -> bytes:
        return b"5 0 obj\nstream\n" + zlib.compress(command) + b"\nendstream\nendobj\n"

    lines = describe_difference(
        "board",
        {"board-PTH-drl_map.pdf": drill_map(b"1 0 0 1 10 20 cm")},
        {"board-PTH-drl_map.pdf": drill_map(b"1 0 0 1 10 25 cm")},
    ).splitlines()

    assert "-1 0 0 1 10 20 cm" in lines and "+1 0 0 1 10 25 cm" in lines


RECORDED = "10.0.7-10.0.7~ubuntu24.04.1"
EXPECTED = {
    "CPL-board.csv": b"Designator,Rotation\r\nR1,0.0\r\n",
    "GERBER-board/board-CuTop.gbr": b"%FSLAX46Y46*%\nX1Y1D03*\n",
}


def _kicad(build: str) -> SimpleNamespace:
    """Stand in for the two version calls ``check_golden`` makes."""
    return SimpleNamespace(
        GetBuildVersion=lambda: build,
        GetMajorMinorVersion=lambda: ".".join(build.split(".")[:2]),
    )


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    """Write one board's expected files, as the recorded KiCad 10 build captured them."""
    root = tmp_path / "golden"
    for name, data in EXPECTED.items():
        path = root / "board" / "10" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "kicad_builds.json").write_text(json.dumps({"10": RECORDED}), "utf-8")
    return root


def _differences(caught: list[warnings.WarningMessage]) -> list[str]:
    """Return the messages of the golden-difference warnings only."""
    return [
        str(warning.message)
        for warning in caught
        if issubclass(warning.category, GoldenDifferenceWarning)
    ]


GERBER_CHANGE = {
    **EXPECTED,
    "GERBER-board/board-CuTop.gbr": b"%FSLAX46Y46*%\nX1Y2D03*\n",
}
CPL_CHANGE = {**EXPECTED, "CPL-board.csv": b"Designator,Rotation\r\nR1,90.0\r\n"}


def test_another_build_warns_about_a_changed_gerber_and_passes(golden: Path) -> None:
    """A newer or different build reports what KiCad plotted differently."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        check_golden("board", _kicad("10.0.4"), GERBER_CHANGE, golden, update="")

    [message] = _differences(caught)
    lines = message.splitlines()
    assert lines[:3] == [
        "Only a warning: this is KiCad 10.0.4, and the expected files come from "
        f"KiCad {RECORDED}, the build CI installs.",
        "board on KiCad 10.0.4 differs from its expected files:",
        "  changed: GERBER-board/board-CuTop.gbr",
    ]
    assert "-X1Y1D03*" in lines and "+X1Y2D03*" in lines
    assert lines[-4:] == [
        "To accept these outputs, regenerate the expected files on "
        f"KiCad {RECORDED}, the build CI installs:",
        regenerate_command("10"),
        "If CI now installs KiCad 10.0.4 instead, move the expected files to it "
        "from this build:",
        regenerate_command("10", "10.0.4"),
    ]


def test_the_recorded_build_fails_on_the_same_change(golden: Path) -> None:
    """On the build that captured the expected files, a changed plot fails."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(pytest.fail.Exception) as failure:
            check_golden("board", _kicad(RECORDED), GERBER_CHANGE, golden, update="")

    lines = str(failure.value).splitlines()
    assert "  changed: GERBER-board/board-CuTop.gbr" in lines
    assert lines[-1] == regenerate_command("10")
    assert _differences(caught) == []


@pytest.mark.parametrize("build", [RECORDED, "10.0.4"], ids=["recorded", "other"])
def test_a_changed_cpl_fails_on_either_build(golden: Path, build: str) -> None:
    """The BOM and CPL are the plugin's own output, so any build must match them."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(pytest.fail.Exception, match="changed: CPL-board.csv"):
            check_golden("board", _kicad(build), CPL_CHANGE, golden, update="")

    assert _differences(caught) == []


@pytest.mark.parametrize("build", [RECORDED, "10.0.4"], ids=["recorded", "other"])
@pytest.mark.parametrize(
    ("published", "message"),
    [
        (
            {"CPL-board.csv": EXPECTED["CPL-board.csv"]},
            "missing: GERBER-board/board-CuTop.gbr",
        ),
        (
            {**EXPECTED, "GERBER-board/board-CuIn1.gbr": b"%FSLAX46Y46*%\n"},
            "unexpected: GERBER-board/board-CuIn1.gbr",
        ),
        (None, "has no expected files"),
    ],
    ids=["unpublished", "no-expected-copy", "no-expected-files"],
)
def test_a_missing_expected_file_fails_on_either_build(
    golden: Path,
    build: str,
    published: Optional[dict[str, bytes]],  # noqa: UP045
    message: str,
) -> None:
    """A file the run did not publish, or one with no expected copy, always fails."""
    if published is None:
        shutil.rmtree(golden / "board")
        published = EXPECTED

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(pytest.fail.Exception, match=message):
            check_golden("board", _kicad(build), published, golden, update="")

    assert _differences(caught) == []


@pytest.mark.parametrize(
    ("recorded", "build", "request_"),
    [
        (RECORDED, RECORDED, "1"),
        (None, "10.0.4", "1"),
        (RECORDED, "10.0.4", "10.0.4"),
    ],
    ids=["recorded-build", "first-capture", "named-build"],
)
def test_update_rewrites_on_the_recorded_build_or_the_one_it_names(
    golden: Path,
    recorded: Optional[str],  # noqa: UP045
    build: str,
    request_: str,
) -> None:
    """Rewriting records the build it ran on, which then becomes the strict one."""
    if recorded is None:
        (golden / "kicad_builds.json").unlink()

    check_golden("board", _kicad(build), GERBER_CHANGE, golden, update=request_)

    assert expected_files(golden / "board" / "10") == GERBER_CHANGE
    assert json.loads((golden / "kicad_builds.json").read_text("utf-8")) == {
        "10": build
    }


@pytest.mark.parametrize(
    ("request_", "reason"),
    [
        ("1", "KICAD_JLCPCB_GOLDEN_UPDATE=1 would move them off that build."),
        ("10.0.8", "KICAD_JLCPCB_GOLDEN_UPDATE names KiCad 10.0.8, but this is"),
    ],
    ids=["unnamed", "another-build"],
)
def test_update_on_another_build_refuses_unless_it_names_this_build(
    golden: Path, request_: str, reason: str
) -> None:
    """The expected files never move off the build CI installs by accident."""
    with pytest.raises(pytest.fail.Exception) as failure:
        check_golden("board", _kicad("10.0.4"), GERBER_CHANGE, golden, update=request_)

    message = str(failure.value)
    assert reason in message
    assert message.splitlines()[-1] == regenerate_command("10", "10.0.4")
    assert expected_files(golden / "board" / "10") == EXPECTED
    assert json.loads((golden / "kicad_builds.json").read_text("utf-8")) == {
        "10": RECORDED
    }


@pytest.mark.parametrize("value", [None, "", "0"], ids=["unset", "empty", "zero"])
def test_update_off_compares_like_an_unset_variable(
    golden: Path,
    value: Optional[str],  # noqa: UP045
) -> None:
    """Unset, empty and 0 all compare: another build warns and rewrites nothing."""
    environ = {} if value is None else {"KICAD_JLCPCB_GOLDEN_UPDATE": value}
    builds = (golden / "kicad_builds.json").read_bytes()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        check_golden(
            "board",
            _kicad("10.0.4"),
            GERBER_CHANGE,
            golden,
            update=update_request(environ),
        )

    assert len(_differences(caught)) == 1
    assert expected_files(golden / "board" / "10") == EXPECTED
    assert (golden / "kicad_builds.json").read_bytes() == builds
