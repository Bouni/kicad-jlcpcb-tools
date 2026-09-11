"""Exercise the correction data contract without importing KiCad or wx."""

import csv
from dataclasses import FrozenInstanceError, replace
import importlib.util
from io import StringIO
import math
from pathlib import Path
import sqlite3
import sys
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def data(monkeypatch):
    """Load the production validator with a scoped module registration."""
    name = "correction_data_validation_tests"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "correction_data.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, 0),
        (-90, -90),
        (450, 450),
        (90.0, 90),
        ("90.0", 90),
        (" +90.000 ", 90),
        ("-0", 0),
        ("9e1", 90),
        (".0", 0),
        (str(-(2**63)), -(2**63)),
        (str(2**63 - 1), 2**63 - 1),
        (-(2**63), -(2**63)),
        (2**63 - 1, 2**63 - 1),
    ],
)
def test_rotation_parses_exact_whole_signed_degrees(data, rotation, expected):
    """Keep supported whole-degree syntax, signs, and exact storage limits."""
    correction = data.validate_correction("SOT-23", rotation, (0, 0))
    assert correction.rotation == expected
    assert isinstance(correction.rotation, int)
    assert correction.offset == (0.0, 0.0)
    assert all(isinstance(value, float) for value in correction.offset)


@pytest.mark.parametrize(
    "rotation",
    [
        "47u",
        "",
        "   ",
        None,
        True,
        False,
        b"90",
        [],
        {},
        complex(90, 0),
        90.5,
        "90.5",
        "90.0000000000000000001",
        "1e-9999",
        "1e99999999999999999999999",
        "1e999999999",
        2**63,
        -(2**63) - 1,
        str(2**63),
        str(-(2**63) - 1),
        float(2**63 - 1),
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",
        "Infinity",
        "-inf",
        "1_0",
        "0x5a",
    ],
)
def test_invalid_rotations_report_original_value_and_location(data, rotation):
    """Reject coercion, truncation, overflow, and issue 531's exact value."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.validate_correction(
            "Capacitor_SMD:C_0805",
            rotation,
            (0, 0),
            source="parts.csv",
            line=7,
            rowid=3,
        )
    (issue,) = raised.value.issues
    assert issue.field == "rotation"
    assert issue.value is rotation
    assert issue.source == "parts.csv"
    assert issue.line == 7
    assert issue.rowid == 3
    assert issue.pattern == "Capacitor_SMD:C_0805"
    assert "whole degrees" in issue.message
    assert "parts.csv, line 7, row 3" in str(raised.value)


@pytest.mark.parametrize("axis", [0, 1])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0.0),
        ("-0", -0.0),
        ("-.125", -0.125),
        (2.75, 2.75),
        (" +2.5e-3 ", 0.0025),
        ("-5e-324", -5e-324),
        (str(sys.float_info.max), sys.float_info.max),
    ],
)
def test_offsets_accept_finite_signed_floats(data, axis, value, expected):
    """Validate both axes with fractions and representable finite extremes."""
    offsets = [0, 0]
    offsets[axis] = value
    correction = data.validate_correction("SOT-23", 90, offsets)
    assert correction.offset[axis] == expected
    assert math.copysign(1, correction.offset[axis]) == math.copysign(1, expected)


@pytest.mark.parametrize("axis", [0, 1])
@pytest.mark.parametrize(
    "value",
    [
        "47u",
        "",
        " ",
        None,
        True,
        False,
        b"1.5",
        [],
        {},
        complex(1, 0),
        "NaN",
        "nan",
        "inf",
        "+Infinity",
        "-inf",
        float("nan"),
        float("inf"),
        "1e309",
        "-1e309",
        10**400,
        "1_0",
    ],
)
def test_invalid_offsets_identify_each_axis(data, axis, value):
    """Reject explicit invalid offsets instead of silently replacing them."""
    offsets = [0, 0]
    offsets[axis] = value
    with pytest.raises(data.CorrectionDataError) as raised:
        data.validate_correction("SOT-23", 90, offsets)
    (issue,) = raised.value.issues
    assert issue.field == ("offset_x", "offset_y")[axis]
    assert issue.value is value


@pytest.mark.parametrize(
    "offset", [None, (), (0,), (0, 0, 0), "00", b"00", {0: 0, 1: 0}]
)
def test_invalid_offset_containers_report_a_structured_error(data, offset):
    """Unexpected database or caller values cannot escape as type errors."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.validate_correction("SOT-23", 0, offset)
    assert raised.value.issues[0].field == "offset"


@pytest.mark.parametrize(
    "pattern",
    [
        "SOT-23|SOT-23-5",
        "^SOT-23$",
        "(?i:SOT-23)",
        r"(?P<prefix>SOT)-(?P=prefix)",
        r"(SOT)-\1",
        " space at both ends ",
        "L'électronique,µF",
        'A"B',
        "SOT\n23",
    ],
)
def test_patterns_preserve_supported_regex_text(data, pattern):
    """Accept expressions compatible with both matcher passes unchanged."""
    assert data.validate_correction(pattern, 0, (0, 0)).pattern == pattern


@pytest.mark.parametrize(
    "pattern",
    [
        None,
        True,
        123,
        b"SOT",
        "",
        " \t\n",
        "[",
        "a{999999999999999999999}",
        "(" * 1000,
    ],
)
def test_invalid_patterns_are_rejected(data, pattern):
    """Reject patterns that are not usable regular expressions."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.validate_correction(pattern, 0, (0, 0))
    assert raised.value.issues[0].field == "pattern"


def test_a_global_flag_pattern_is_usable(data):
    """A leading (?i) is a valid regex, and now survives to match with.

    It only ever failed validation because the discarded anchored pass wrapped
    the pattern in "(?:...)$", which moves the flag off the front of the
    expression -- where Python requires it to be.
    """
    correction = data.Correction("(?i)SOT-23", 90, (0, 0))
    assert data.find_correction((correction,), "Package:sot-23") is correction


def test_validation_aggregates_all_fields(data):
    """Report each invalid field in one repair attempt."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.validate_correction("[", "47u", ("", "NaN"))
    assert [issue.field for issue in raised.value.issues] == [
        "pattern",
        "rotation",
        "offset_x",
        "offset_y",
    ]


def test_correction_and_issue_records_are_immutable(data):
    """Validated snapshots and diagnostics cannot be changed accidentally."""
    correction = data.validate_correction("SOT", 90, (0, 0))
    issue = data.CorrectionIssue("rotation", "47u", "whole degrees required")
    with pytest.raises(FrozenInstanceError):
        correction.rotation = 47
    with pytest.raises(FrozenInstanceError):
        issue.field = "pattern"


@pytest.mark.parametrize(
    "text",
    [
        "Pattern,Rotation,Offset X,Offset Y\nSOT,90,1.5,-2\n",
        '"Pattern","Rotation","Offset X","Offset Y"\r\n"SOT","90","1.5","-2"\r\n',
        '"Pattern","Rotation", "Offset X","Offset Y"\nSOT,90,1.5,-2\n',
        "Offset Y, Rotation, Pattern, Offset X\n-2,90,SOT,1.5\n",
        "\ufeff\n\npattern,ROTATION,Offset   X,offset y\r\nSOT,90,1.5,-2\r\n",
    ],
)
def test_current_remote_and_reordered_headers(data, text):
    """Recognize exporter and remote syntax with safe header mapping."""
    assert data.parse_corrections_csv(text) == (
        data.Correction("SOT", 90, (1.5, -2.0)),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Footprint pattern,Correction\nSOT,-90\n", ("SOT", -90, (0.0, 0.0))),
        ("Correction,Footprint pattern\n90,SOT\n", ("SOT", 90, (0.0, 0.0))),
        ("Pattern,Rotation,Offset X,Offset Y\nSOT,90\n", ("SOT", 90, (0.0, 0.0))),
        ("Pattern,Rotation,Offset X,Offset Y\nSOT,90,1.5\n", ("SOT", 90, (1.5, 0.0))),
        ("Pattern,Rotation,Offset Y,Offset X\nSOT,90,1.5\n", ("SOT", 90, (0.0, 1.5))),
        ("Offset X,Pattern,Rotation,Offset Y\n1.5,SOT,90\n", ("SOT", 90, (1.5, 0.0))),
    ],
)
def test_legacy_rows_default_only_omitted_trailing_offsets(data, text, expected):
    """Support historical records without defaulting missing required fields."""
    assert data.parse_corrections_csv(text) == (data.Correction(*expected),)


@pytest.mark.parametrize(
    "header",
    [
        "Pattern,Rotation",
        "Footprint pattern,Correction",
        "Pattern,Rotation,Offset X,Offset Y",
    ],
)
def test_header_only_file_is_a_noop(data, header):
    """A recognized empty export is a valid empty batch."""
    assert data.parse_corrections_csv(header + "\n\n  \n") == ()


@pytest.mark.parametrize("text", ["", "\n\r\n \t\n", "\ufeff", "\ufeff\n"])
def test_empty_file_requires_a_header(data, text):
    """Distinguish an absent file format from a valid empty export."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv(text, "empty.csv")
    assert raised.value.issues[0].field == "header"
    assert raised.value.issues[0].source == "empty.csv"


@pytest.mark.parametrize(
    "header",
    [
        "Pattern,Rotatoin",
        "Pattern,Rotation,Unknown",
        "Pattern,Pattern,Rotation",
        "Footprint pattern,Pattern,Correction",
        "Pattern,Rotation,Correction",
        "Pattern,Rotation,Offset X,offset x",
        "Rotation,Offset X,Offset Y",
        "Pattern,Offset X,Offset Y",
        "Pattern,Rotation,",
        ",,,",
        "SOT,90",
    ],
)
def test_invalid_headers_reject_input_before_records(data, header):
    """Reject unknown, duplicated, missing, and accidental data headers."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv(header + "\nSOT,90\n", "bad.csv")
    assert all(issue.field == "header" for issue in raised.value.issues)
    assert all(issue.line == 1 for issue in raised.value.issues)


@pytest.mark.parametrize(
    ("row", "fields"),
    [
        ("SOT", ["rotation"]),
        ("SOT,", ["rotation"]),
        ("SOT,90,", ["offset_x"]),
        ("SOT,90,1,", ["offset_y"]),
        ("SOT,90,,1", ["offset_x"]),
        (",90,0,0", ["pattern"]),
        (",,,", ["pattern", "rotation", "offset_x", "offset_y"]),
        ('""', ["pattern", "rotation"]),
        ("SOT,90,0,0,extra", ["csv"]),
    ],
)
def test_explicit_blank_and_malformed_rows_never_become_defaults(data, row, fields):
    """Differentiate omitted legacy offsets from corrupt or incomplete data."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv("Pattern,Rotation,Offset X,Offset Y\n" + row)
    assert [issue.field for issue in raised.value.issues] == fields


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("Pattern,Offset X,Rotation\nSOT,0", "rotation"),
        ("Rotation,Offset X,Pattern\n90,0", "pattern"),
    ],
)
def test_reordered_header_does_not_default_missing_required_values(data, text, field):
    """Missing physical trailing columns are resolved by their header names."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv(text)
    assert [issue.field for issue in raised.value.issues] == [field]


def test_blank_lines_preserve_physical_line_diagnostics(data):
    """Locate bad rows correctly after blanks and quoted multiline records."""
    text = '\nPattern,Rotation\n\n"SOT\n23",90\n \nCAP,47u\n'
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv(text, "corrections.csv")
    (issue,) = raised.value.issues
    assert issue.line == 7
    assert issue.pattern == "CAP"
    assert issue.value == "47u"
    assert "corrections.csv, line 7, pattern 'CAP'" in str(raised.value)


@pytest.mark.parametrize("invalid_row", ['"SOT,90', '"SOT"bad,90'])
def test_malformed_quoting_rejects_whole_input(data, invalid_row):
    """A late CSV syntax error must not return earlier valid corrections."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv("Pattern,Rotation\nVALID,90\n" + invalid_row)
    (issue,) = raised.value.issues
    assert issue.field == "csv"
    assert issue.line == 3


def test_malformed_header_quoting_is_a_structured_error(data):
    """Header parsing failures use the same error type as data failures."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv('"Pattern,Rotation\nSOT,90')
    assert raised.value.issues[0].field == "csv"


@pytest.mark.parametrize("invalid_position", [0, 1, 2])
def test_entire_batch_validates_regardless_of_invalid_row_position(
    data, invalid_position
):
    """Issue 531 fails before callers receive any records to persist."""
    rows = ["FIRST,90", "MIDDLE,-90", "LAST,180"]
    rows[invalid_position] = "CAP,47u"
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv("Pattern,Rotation\n" + "\n".join(rows))
    assert raised.value.issues[0].line == invalid_position + 2


def test_all_invalid_records_are_reported_in_input_order(data):
    """Give one complete validation result, including a later syntax failure."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv('Pattern,Rotation\nA,47u\nB,90.1\n"unclosed')
    assert [(issue.line, issue.field) for issue in raised.value.issues] == [
        (2, "rotation"),
        (3, "rotation"),
        (4, "csv"),
    ]


def test_valid_duplicates_remain_in_input_order(data):
    """Leave overwrite or add-missing policy to the atomic storage boundary."""
    corrections = data.parse_corrections_csv("Pattern,Rotation\nSOT,90\nSOT,-90")
    assert corrections == (
        data.Correction("SOT", 90, (0.0, 0.0)),
        data.Correction("SOT", -90, (0.0, 0.0)),
    )


@pytest.mark.parametrize("rows", ["SOT,47u\nSOT,90", "SOT,90\nSOT,47u"])
def test_duplicate_resolution_cannot_hide_invalid_values(data, rows):
    """Validate even rows that an overwrite or remote update might skip."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv("Pattern,Rotation\n" + rows)
    assert raised.value.issues[0].value == "47u"


def test_csv_roundtrip_preserves_patterns_and_typed_values(data):
    """Read the actual export format with quotes, Unicode, and backslashes."""
    stream = StringIO(newline="")
    writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
    writer.writerow(["Pattern", "Rotation", "Offset X", "Offset Y"])
    expected = (
        data.Correction("  SOT-23  ", -450, (-1.25, 0.125)),
        data.Correction('L\'électronique,"µF"', 90, (0.0, -0.0)),
        data.Correction(r"Package:C_\d+", 180, (1.5, -2.25)),
        data.Correction("SOT\n23", 0, (0.0, 0.0)),
    )
    for correction in expected:
        writer.writerow([correction.pattern, correction.rotation, *correction.offset])
    assert data.parse_corrections_csv(stream.getvalue()) == expected


def test_unquoted_pattern_whitespace_is_preserved(data):
    """Header whitespace compatibility cannot change a matching expression."""
    text = 'Pattern, Rotation, "Offset X", "Offset Y"\n  SOT  ,90\n'
    assert data.parse_corrections_csv(text)[0].pattern == "  SOT  "


def test_mixed_legacy_and_current_rows(data):
    """The remote feed can mix two-field and four-field records."""
    text = '"Pattern","Rotation", "Offset X","Offset Y"\nA,90\nB,180,1.5\nC,-90,-1,2\n'
    assert data.parse_corrections_csv(text) == (
        data.Correction("A", 90, (0.0, 0.0)),
        data.Correction("B", 180, (1.5, 0.0)),
        data.Correction("C", -90, (-1.0, 2.0)),
    )


@pytest.mark.parametrize("value", [None, b"Pattern,Rotation\nSOT,90", []])
def test_parser_requires_decoded_text(data, value):
    """Unexpected caller values produce structured input errors."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.parse_corrections_csv(value)
    assert raised.value.issues[0].field == "csv"


@pytest.mark.parametrize(
    ("pattern", "rotation", "offset", "field"),
    [
        ("C1", "47u", (0, 0), "rotation"),
        ("C1", "90.5", (0, 0), "rotation"),
        ("C1", 2**63, (0, 0), "rotation"),
        ("C1", -(2**63) - 1, (0, 0), "rotation"),
        ("C1", float("inf"), (0, 0), "rotation"),
        ("C1", True, (0, 0), "rotation"),
        ("[", 0, (0, 0), "pattern"),
        (" ", 0, (0, 0), "pattern"),
        (123, 0, (0, 0), "pattern"),
        ("C1", 0, (float("nan"), 0), "offset_x"),
        ("C1", 0, (0, "47u"), "offset_y"),
        ("C1", 0, [0], "offset"),
    ],
)
def test_direct_constructor_rejects_invalid_values(
    data: ModuleType, pattern: object, rotation: object, offset: object, field: str
) -> None:
    """Every constructed value must satisfy the correction contract."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.Correction(pattern, rotation, offset)
    assert raised.value.issues[0].field == field


def test_direct_constructor_aggregates_field_issues(data: ModuleType) -> None:
    """Raw constructor diagnostics retain all values for contextual repair."""
    with pytest.raises(data.CorrectionDataError) as raised:
        data.Correction("[", "47u", ("", "NaN"))
    assert [(issue.field, issue.value) for issue in raised.value.issues] == [
        ("pattern", "["),
        ("rotation", "47u"),
        ("offset_x", ""),
        ("offset_y", "NaN"),
    ]


def test_direct_constructor_normalizes_and_detaches_offsets(data: ModuleType) -> None:
    """Caller mutation cannot invalidate a correction or a stored snapshot."""
    offsets = ["-0", "0.125"]
    correction = data.Correction("  C1  ", "9e1", offsets)
    offsets[0] = "47u"
    offsets.append("NaN")
    assert correction.pattern == "  C1  "
    assert correction.rotation == 90
    assert type(correction.rotation) is int
    assert type(correction.offset) is tuple
    assert correction.offset == (-0.0, 0.125)
    assert all(type(value) is float for value in correction.offset)
    assert math.copysign(1, correction.offset[0]) == -1
    assert hash(correction) == hash(data.Correction("  C1  ", 90, (-0.0, 0.125)))


@pytest.mark.parametrize(
    "changes",
    [
        {"rotation": "47u"},
        {"rotation": 90.5},
        {"pattern": "["},
        {"offset": (0, float("inf"))},
    ],
)
def test_dataclass_replace_cannot_bypass_validation(
    data: ModuleType, changes: dict[str, object]
) -> None:
    """Replacing a field invokes the same invariant as initial construction."""
    correction = data.Correction("C1", 90, (1.0, 2.0))
    with pytest.raises(data.CorrectionDataError):
        replace(correction, **changes)
    assert correction == data.Correction("C1", 90, (1.0, 2.0))


def test_dataclass_replace_normalizes_valid_inputs(data: ModuleType) -> None:
    """Replacing fields produces another independent immutable value."""
    offsets = ["-1.25", "2.5"]
    correction = replace(
        data.Correction("C1", 90, (0.0, 0.0)), rotation="-9e1", offset=offsets
    )
    offsets[0] = "47u"
    assert correction == data.Correction("C1", -90, (-1.25, 2.5))


def test_dataclass_replace_pattern_updates_the_matching_expression(
    data: ModuleType,
) -> None:
    """A replacement uses its new pattern while the original remains usable."""
    original = data.Correction("OLD", 90, (1.25, -2.5))
    replacement = replace(original, pattern="NEW")
    fallback = data.Correction("prefix", -90, (0, 0))

    assert data.find_correction((fallback, replacement), "only-NEW") is replacement
    assert data.find_correction((replacement,), "prefix-NEW-suffix") is replacement
    assert data.find_correction((replacement,), "prefix-OLD") is None
    assert data.find_correction((replacement,), "prefix-OLD-suffix") is None
    assert data.find_correction((original,), "prefix-OLD") is original
    assert data.find_correction((original,), "prefix-OLD-suffix") is original
    assert data.find_correction((original,), "NEW") is None


def test_replaced_correction_preserves_public_value_contract(data: ModuleType) -> None:
    """Matching state does not enter equality, hashing, display or persistence."""
    correction = replace(data.Correction("OLD", 90, (1.25, -2.5)), pattern="NEW")
    equivalent = data.Correction("NEW", "9e1", ["1.25", "-2.5"])

    assert correction is not equivalent
    assert correction == equivalent
    assert correction != replace(correction, pattern="OLD")
    assert correction != replace(correction, rotation=0)
    assert correction != replace(correction, offset=(0, 0))
    assert hash(correction) == hash(equivalent) == hash(("NEW", 90, (1.25, -2.5)))
    assert {correction: "selected"}[equivalent] == "selected"
    assert (
        repr(correction)
        == "Correction(pattern='NEW', rotation=90, offset=(1.25, -2.5))"
    )
    assert str(correction) == "90°, 1.25/-2.5"
    assert correction.db_row() == ("NEW", 90, 1.25, -2.5)
    assert correction.csv_row() == ("NEW", 90, 1.25, -2.5)
    assert correction.editor_values() == ("NEW", "90", "1.25", "-2.5")
    with pytest.raises(FrozenInstanceError):
        correction.pattern = "OLD"


def test_parse_returns_optional_without_exposing_invalid_corrections(
    data: ModuleType,
) -> None:
    """Expected invalid input is represented by absence, never a partial value."""
    assert data.Correction.parse("C1", "47u", (0, 0)) is None
    assert data.Correction.parse("[", 90, (0, 0)) is None
    assert data.Correction.parse("C1", 90, (0, "NaN")) is None
    assert data.Correction.parse("C1", "-9e1", ["1.25", "-0"]) == data.Correction(
        "C1", -90, (1.25, -0.0)
    )


def test_parse_does_not_hide_unexpected_programming_errors(
    data: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Programming failures must escape the optional input parsing boundary."""

    def fail_validation(pattern: object, rotation: object, offset: object) -> None:
        raise RuntimeError("unexpected constructor failure")

    monkeypatch.setattr(data, "_validated_values", fail_validation)
    with pytest.raises(RuntimeError, match="unexpected constructor failure"):
        data.Correction.parse("C1", 90, (0, 0))


@pytest.mark.parametrize("rotation", [-(2**63), 2**63 - 1, 9007199254740993, -450, 0])
@pytest.mark.parametrize(
    "offset",
    [(-0.0, 0.0), (0.12345678901234566, -5e-324), (sys.float_info.max, -1.25)],
)
def test_serialization_roundtrips_exact_values(
    data: ModuleType, rotation: int, offset: tuple[float, float]
) -> None:
    """Database, CSV and editor boundaries share lossless canonical values."""
    pattern = '  L\'électronique,"µF"\\d+\n  '
    correction = data.Correction(pattern, rotation, offset)
    assert correction.db_row() == (pattern, rotation, *offset)
    assert correction.csv_row() == correction.db_row()
    assert correction.editor_values() == (
        pattern,
        str(rotation),
        str(offset[0]),
        str(offset[1]),
    )
    assert str(correction) == f"{rotation}°, {offset[0]}/{offset[1]}"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE corrections(regex, rotation, offset_x, offset_y)"
        )
        connection.execute(
            "INSERT INTO corrections VALUES (?, ?, ?, ?)", correction.db_row()
        )
        row = connection.execute("SELECT * FROM corrections").fetchone()
    assert data.Correction(row[0], row[1], row[2:]) == correction
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(("Pattern", "Rotation", "Offset X", "Offset Y"))
    writer.writerow(correction.csv_row())
    (csv_correction,) = data.parse_corrections_csv(stream.getvalue())
    assert csv_correction == correction
    assert math.copysign(1, csv_correction.offset[0]) == math.copysign(1, offset[0])
    editor = correction.editor_values()
    assert data.Correction.parse(editor[0], editor[1], editor[2:]) == correction
