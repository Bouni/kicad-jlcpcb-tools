"""Validate correction values and CSV files without GUI or database dependencies."""

from collections.abc import Callable, Iterable, Iterator, Sequence
import csv
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from io import StringIO
import math
import re
from typing import Literal, Optional, Union

from .lcsc import is_lcsc_part, normalize_lcsc

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
    _suffix_regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __init__(self, pattern: object, rotation: object, offset: object) -> None:
        """Enforce the value contract at every construction or replacement."""
        pattern, rotation, offset, regex, suffix_regex = _validated_values(
            pattern, rotation, offset
        )
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "rotation", rotation)
        object.__setattr__(self, "offset", offset)
        object.__setattr__(self, "_regex", regex)
        object.__setattr__(self, "_suffix_regex", suffix_regex)

    @classmethod
    def parse(
        cls, pattern: object, rotation: object, offset: object
    ) -> Optional["Correction"]:  # noqa: UP045
        """Return a normalized correction, or None for invalid input values."""
        try:
            return cls(pattern, rotation, offset)
        except CorrectionDataError:
            return None

    @property
    def key(self) -> str:
        """Return the text this rule is stored and listed under."""
        return self.pattern

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


KIND_FOOTPRINT: Literal["footprint"] = "footprint"
KIND_LCSC: Literal["lcsc"] = "lcsc"
CorrectionKind = Literal["footprint", "lcsc"]


def _lcsc_key(value: object) -> str:
    """Return the canonical part number, rejecting anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError("expected an LCSC part number")
    if not is_lcsc_part(value):
        raise ValueError("expected an LCSC part number")
    return normalize_lcsc(value)


@dataclass(frozen=True, init=False)
class LcscCorrection:
    """A validated correction for exactly one LCSC part number.

    The package JLC assembles belongs to the part number, not to the name KiCad
    gives the footprint, so two parts on one footprint name can need different
    rotations. No pattern over reference, value or footprint text can tell them
    apart; an exact per-part key can. The key is stored and compared in the
    canonical form from ``normalize_lcsc``.
    """

    lcsc: str
    rotation: int
    offset: tuple[float, float]

    def __init__(self, lcsc: object, rotation: object, offset: object) -> None:
        """Enforce the value contract at every construction or replacement."""
        lcsc, rotation, offset = _validated_lcsc_values(lcsc, rotation, offset)
        object.__setattr__(self, "lcsc", lcsc)
        object.__setattr__(self, "rotation", rotation)
        object.__setattr__(self, "offset", offset)

    @classmethod
    def parse(
        cls, lcsc: object, rotation: object, offset: object
    ) -> Optional["LcscCorrection"]:  # noqa: UP045
        """Return a normalized correction, or None for invalid input values."""
        try:
            return cls(lcsc, rotation, offset)
        except CorrectionDataError:
            return None

    @property
    def key(self) -> str:
        """Return the text this rule is stored and listed under."""
        return self.lcsc

    def db_row(self) -> tuple[str, int, float, float]:
        """Return native SQLite values in the lcsc_correction column order."""
        return self.lcsc, self.rotation, self.offset[0], self.offset[1]

    def editor_values(self) -> tuple[str, str, str, str]:
        """Return lossless text for the part number, rotation and two offsets."""
        return (
            self.lcsc,
            str(self.rotation),
            str(self.offset[0]),
            str(self.offset[1]),
        )

    def __str__(self) -> str:
        """Format the correction consistently for display without rounding."""
        return f"{self.rotation}°, {self.offset[0]}/{self.offset[1]}"


AnyCorrection = Union[Correction, LcscCorrection]


def correction_kind(correction: AnyCorrection) -> CorrectionKind:
    """Name the table a validated correction belongs to."""
    return KIND_LCSC if isinstance(correction, LcscCorrection) else KIND_FOOTPRINT


@dataclass(frozen=True)
class CorrectionMatch:
    """A selected correction and the part field that matched it."""

    correction: AnyCorrection
    source: Literal["lcsc", "ref", "val", "fpt"]


def find_correction(
    corrections: Sequence[AnyCorrection], value: str
) -> Optional[Correction]:  # noqa: UP045
    """Prefer a suffix match, then the first unanchored match in input order.

    Part-number rules carry no pattern and are skipped here; match_correction
    resolves them by exact key before any pattern is tried.
    """
    for correction in corrections:
        if isinstance(correction, Correction) and correction._suffix_regex.search(
            value
        ):
            return correction
    for correction in corrections:
        if isinstance(correction, Correction) and correction._regex.search(value):
            return correction
    return None


def match_correction(
    corrections: Sequence[AnyCorrection],
    reference: str,
    value: str,
    footprint: str,
    lcsc: object = "",
) -> Optional[CorrectionMatch]:  # noqa: UP045
    """Resolve the exact part number, then reference, value and footprint.

    A rule for the part itself outranks every pattern, including a rule of zero
    degrees and zero offset, which switches a family correction off for that
    one part.
    """
    key = normalize_lcsc(lcsc)
    if key:
        for correction in corrections:
            if isinstance(correction, LcscCorrection) and correction.lcsc == key:
                return CorrectionMatch(correction, "lcsc")
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


def _validated_numbers(
    rotation: object, offset: object, issue: Callable[[str, object, str], None]
) -> tuple[Optional[int], list[float]]:  # noqa: UP045
    """Validate the rotation and both offsets, reporting every problem found."""
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
    return validated_rotation, validated_offsets


def _validated_values(
    pattern: object, rotation: object, offset: object
) -> tuple[str, int, tuple[float, float], re.Pattern[str], re.Pattern[str]]:
    """Validate all fields once and return normalized constructor values.

    Pattern text is preserved exactly. Both matching expressions are compiled
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
    suffix_regex = None
    if not isinstance(pattern, str) or not pattern.strip():
        issue("pattern", pattern, "expected a nonempty regular expression")
    else:
        try:
            regex = re.compile(pattern)
            suffix_regex = re.compile(f"(?:{pattern})$")
        except (re.error, OverflowError, RecursionError) as error:
            issue("pattern", pattern, f"invalid correction regular expression: {error}")

    validated_rotation, validated_offsets = _validated_numbers(rotation, offset, issue)

    if issues:
        raise CorrectionDataError(issues)
    # Field validation above guarantees these types when there are no issues.
    assert isinstance(pattern, str)
    assert validated_rotation is not None
    assert regex is not None
    assert suffix_regex is not None
    return (
        pattern,
        validated_rotation,
        (validated_offsets[0], validated_offsets[1]),
        regex,
        suffix_regex,
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


def _validated_lcsc_values(
    lcsc: object, rotation: object, offset: object
) -> tuple[str, int, tuple[float, float]]:
    """Validate a part-number rule once and return normalized constructor values.

    The part number is reduced to its canonical form and must be a bare
    number such as C12345: an exact-match key that is really a pattern would
    silently never fire. Rotation and offsets follow the pattern rules.
    """
    issues = []

    def issue(field: str, value: object, message: str) -> None:
        issues.append(CorrectionIssue(field, value, message))

    key = None
    try:
        key = _lcsc_key(lcsc)
    except (TypeError, ValueError):
        issue("lcsc", lcsc, "expected an LCSC part number such as C12345")
    validated_rotation, validated_offsets = _validated_numbers(rotation, offset, issue)

    if issues:
        raise CorrectionDataError(issues)
    assert key is not None
    assert validated_rotation is not None
    return key, validated_rotation, (validated_offsets[0], validated_offsets[1])


def validate_lcsc_correction(
    lcsc: object,
    rotation: object,
    offset: object,
    *,
    source: str = "",
    line: Optional[int] = None,  # noqa: UP045
    rowid: Optional[int] = None,  # noqa: UP045
) -> LcscCorrection:
    """Construct a part-number correction and attach source locations to errors."""
    try:
        return LcscCorrection(lcsc, rotation, offset)
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
