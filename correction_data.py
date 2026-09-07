"""Validate correction values and CSV files without GUI or database dependencies."""

from collections.abc import Iterable, Iterator, Sequence
import csv
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from io import StringIO
import math
import re
from typing import Literal, Optional

_MIN_ROTATION = -(2**63)
_MAX_ROTATION = 2**63 - 1
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_HEADER_FIELDS = {
    "pattern": "pattern",
    "footprint pattern": "pattern",
    "rotation": "rotation",
    "correction": "rotation",
    "offset x": "offset_x",
    "offset y": "offset_y",
}


@dataclass(frozen=True, init=False)
class Correction:
    """A validated correction ready for persistence or matching."""

    pattern: str
    rotation: int
    offset: tuple[float, float]
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __init__(self, pattern: object, rotation: object, offset: object) -> None:
        """Enforce the value contract at every construction or replacement."""
        pattern, rotation, offset, regex = _validated_values(pattern, rotation, offset)
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "rotation", rotation)
        object.__setattr__(self, "offset", offset)
        object.__setattr__(self, "_regex", regex)

    @classmethod
    def parse(
        cls, pattern: object, rotation: object, offset: object
    ) -> Optional["Correction"]:  # noqa: UP045
        """Return a normalized correction, or None for invalid input values."""
        try:
            return cls(pattern, rotation, offset)
        except CorrectionDataError:
            return None

    def db_row(self) -> tuple[str, int, float, float]:
        """Return native SQLite values in the correction table column order."""
        return self.pattern, self.rotation, self.offset[0], self.offset[1]

    def csv_row(self) -> tuple[str, int, float, float]:
        """Return values whose CSV representation retains numeric precision."""
        return self.db_row()

    def editor_values(self) -> tuple[str, str, str, str]:
        """Return lossless text for the pattern, rotation and two offset fields."""
        return (
            self.pattern,
            str(self.rotation),
            str(self.offset[0]),
            str(self.offset[1]),
        )

    def __str__(self) -> str:
        """Format the correction consistently for display without rounding."""
        return f"{self.rotation}°, {self.offset[0]}/{self.offset[1]}"


@dataclass(frozen=True)
class CorrectionMatch:
    """A selected correction and the footprint field that matched it."""

    correction: Correction
    source: Literal["ref", "val", "fpt"]


def find_correction(
    corrections: Sequence[Correction], value: str
) -> Optional[Correction]:  # noqa: UP045
    """Return the correction consuming the most of the value; ties keep order."""
    best = None
    best_length = -1
    for correction in corrections:
        match = correction._regex.search(value)
        if match is None:
            continue
        length = len(match.group(0))
        if length > best_length:
            best = correction
            best_length = length
    return best


def match_correction(
    corrections: Sequence[Correction], reference: str, value: str, footprint: str
) -> Optional[CorrectionMatch]:  # noqa: UP045
    """Resolve reference, then value, then footprint using one matching policy."""
    targets: tuple[tuple[Literal["ref", "val", "fpt"], str], ...] = (
        ("ref", reference),
        ("val", value),
        ("fpt", footprint),
    )
    for source, target in targets:
        correction = find_correction(corrections, target)
        if correction is not None:
            return CorrectionMatch(correction, source)
    return None


@dataclass(frozen=True)
class CorrectionIssue:
    """Describe an invalid value together with its repair location."""

    field: str
    value: object
    message: str
    source: str = ""
    # KiCad can evaluate these annotations under Python 3.9.
    line: Optional[int] = None  # noqa: UP045
    pattern: Optional[str] = None  # noqa: UP045
    rowid: Optional[int] = None  # noqa: UP045

    def __str__(self) -> str:
        """Format an actionable diagnostic without losing the original value."""
        location = [self.source] if self.source else []
        if self.line is not None:
            location.append(f"line {self.line}")
        if self.rowid is not None:
            location.append(f"row {self.rowid}")
        if self.pattern is not None:
            location.append(f"pattern {self.pattern!r}")
        prefix = ", ".join(location)
        detail = f"{self.field}: {self.value!r}; {self.message}"
        return f"{prefix}: {detail}" if prefix else detail


class CorrectionDataError(ValueError):
    """Report all correction issues found before an operation can proceed."""

    def __init__(self, issues: Iterable[CorrectionIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


def _number_text(value: object) -> str:
    """Return decimal text only for supported, explicitly numeric values."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("expected a number")
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        raise ValueError("expected a number")
    return text


def _rotation(value: object) -> int:
    """Parse whole degrees exactly, without float rounding or truncation."""
    number = Decimal(_number_text(value))
    if (
        not number.is_finite()
        or number < _MIN_ROTATION
        or number > _MAX_ROTATION
        or number != number.to_integral_value()
    ):
        raise ValueError("rotation is not a representable whole number")
    return int(number)


def _offset(value: object) -> float:
    """Parse a finite offset suitable for SQLite REAL storage."""
    number = float(_number_text(value))
    if not math.isfinite(number):
        raise ValueError("offset is not finite")
    return number


def _validated_values(
    pattern: object, rotation: object, offset: object
) -> tuple[str, int, tuple[float, float], re.Pattern[str]]:
    """Validate all fields once and return normalized constructor values.

    Pattern text is preserved exactly and its matching expression is compiled
    and retained. Rotations are signed whole degrees within SQLite's
    integer range; offsets must be finite floats. No invalid value is defaulted.
    """
    issues = []

    def issue(field: str, value: object, message: str) -> None:
        issues.append(
            CorrectionIssue(
                field,
                value,
                message,
                pattern=pattern if isinstance(pattern, str) else None,
            )
        )

    regex = None
    if not isinstance(pattern, str) or not pattern.strip():
        issue("pattern", pattern, "expected a nonempty regular expression")
    else:
        try:
            regex = re.compile(pattern)
        except (re.error, OverflowError, RecursionError) as error:
            issue("pattern", pattern, f"invalid correction regular expression: {error}")

    validated_rotation = None
    try:
        validated_rotation = _rotation(rotation)
    except (TypeError, ValueError, InvalidOperation, OverflowError):
        issue(
            "rotation",
            rotation,
            f"expected finite whole degrees between {_MIN_ROTATION} and {_MAX_ROTATION}",
        )

    validated_offsets = []
    if not isinstance(offset, (tuple, list)) or len(offset) != 2:
        issue("offset", offset, "expected exactly two offsets (X and Y)")
    else:
        for field, value in zip(("offset_x", "offset_y"), offset):
            try:
                validated_offsets.append(_offset(value))
            except (TypeError, ValueError, OverflowError):
                issue(field, value, "expected a finite numeric offset")

    if issues:
        raise CorrectionDataError(issues)
    # Field validation above guarantees these types when there are no issues.
    assert isinstance(pattern, str)
    assert validated_rotation is not None
    assert regex is not None
    return (
        pattern,
        validated_rotation,
        (validated_offsets[0], validated_offsets[1]),
        regex,
    )


def validate_correction(
    pattern: object,
    rotation: object,
    offset: object,
    *,
    source: str = "",
    line: Optional[int] = None,  # noqa: UP045
    rowid: Optional[int] = None,  # noqa: UP045
) -> Correction:
    """Construct a correction and attach source locations to field errors."""
    try:
        return Correction(pattern, rotation, offset)
    except CorrectionDataError as error:
        raise CorrectionDataError(
            replace(issue, source=source, line=line, rowid=rowid)
            for issue in error.issues
        ) from error


def _header_columns(row: list[str], source: str, line: int) -> list[str]:
    """Map known header aliases and reject ambiguous or incomplete headers."""
    columns = []
    issues = []
    for name in row:
        field = _HEADER_FIELDS.get(" ".join(name.split()).casefold())
        if field is None:
            issues.append(
                CorrectionIssue(
                    "header", name, "unknown correction column", source, line
                )
            )
        elif field in columns:
            issues.append(
                CorrectionIssue(
                    "header", name, "duplicate correction column", source, line
                )
            )
        columns.append(field)
    for field in ("pattern", "rotation"):
        if field not in columns:
            issues.append(
                CorrectionIssue(
                    "header", row, f"missing required {field} column", source, line
                )
            )
    if issues:
        raise CorrectionDataError(issues)
    return columns


def _csv_rows(
    stream: StringIO,
    *,
    source: str,
    skipinitialspace: bool = False,
    line_offset: int = 0,
) -> Iterator[tuple[list[str], int, int]]:
    """Yield nonblank CSV records with their starting physical line numbers."""
    reader = csv.reader(stream, strict=True, skipinitialspace=skipinitialspace)
    while True:
        start = stream.tell()
        line = line_offset + reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as error:
            raise CorrectionDataError(
                (CorrectionIssue("csv", None, str(error), source, line),)
            ) from error
        # Ignore physical whitespace-only lines, but validate explicit empty
        # fields such as `""` and delimiter-only records like `,,,`.
        if not stream.getvalue()[start : stream.tell()].strip():
            continue
        yield row, line, line_offset + reader.line_num


def parse_corrections_csv(text: str, source: str = "") -> tuple[Correction, ...]:
    """Parse and validate an entire current, historical, or remote CSV file.

    Header names determine column positions. Missing trailing offsets default
    to zero, while explicit blank fields and missing required values fail.
    Duplicate patterns remain in input order so callers can apply their policy
    only after every row has validated. Any error rejects the complete input.
    """
    if not isinstance(text, str):
        raise CorrectionDataError(
            (CorrectionIssue("csv", text, "expected decoded CSV text", source),)
        )
    stream = StringIO(text.removeprefix("\ufeff"), newline="")
    # The remote source has spaces before a quoted header. Restrict this
    # tolerance to headers so leading spaces in user patterns stay unchanged.
    header = next(_csv_rows(stream, source=source, skipinitialspace=True), None)
    if header is None:
        raise CorrectionDataError(
            (
                CorrectionIssue(
                    "header", None, "expected a correction CSV header", source
                ),
            )
        )
    row, line, header_end_line = header
    columns = _header_columns(row, source, line)
    corrections = []
    issues = []
    try:
        for row, line, _end_line in _csv_rows(
            stream, source=source, line_offset=header_end_line
        ):
            if len(row) > len(columns):
                issues.append(
                    CorrectionIssue(
                        "csv", row, "more values than header columns", source, line
                    )
                )
                continue
            values = dict(zip(columns, row))
            try:
                corrections.append(
                    validate_correction(
                        values.get("pattern"),
                        values.get("rotation"),
                        (values.get("offset_x", 0), values.get("offset_y", 0)),
                        source=source,
                        line=line,
                    )
                )
            except CorrectionDataError as error:
                issues.extend(error.issues)
    except CorrectionDataError as error:
        issues.extend(error.issues)
    if issues:
        raise CorrectionDataError(issues)
    return tuple(corrections)
