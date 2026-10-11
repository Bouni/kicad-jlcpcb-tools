"""Findings from the checks that run before fabrication files are generated.

Each check's result becomes a list of findings. Generation collects the
findings of every check, then asks for one decision in the review pane when
any of them needs one. Nothing here prompts or imports wx.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import enum
import logging


class Severity(enum.IntEnum):
    """How much a finding matters, most severe first."""

    ERROR = 0  # a serious issue, likely to cost a board
    WARNING = 1  # worth a look before generating
    INFO = 2  # logged; listed only when the pane opens for something else


HEADINGS = {
    Severity.ERROR: "Serious issues",
    Severity.WARNING: "Warnings",
    Severity.INFO: "Info",
}


@dataclass(frozen=True)
class Finding:
    """One thing a check found, as the review pane lists it."""

    severity: Severity
    check: str  # display name of the check, e.g. "DRC"
    title: str  # the list row's single line
    detail: str = ""  # shown below the list when the row is selected
    references: tuple[str, ...] = ()  # footprint references, empty when none


_PLACEHOLDER_DETAIL = (
    "The order-number setting is on, so JLCPCB expects exactly one place on "
    "the silkscreen for its order or serial number: the text JLCJLCJLCJLC, a "
    "filled 5, 8 or 10 mm square for a 2D barcode, or a filled 10 x 2 mm "
    "rectangle for a serial number."
)


def value_mismatch_findings(
    groups: Mapping[str, Mapping[str, Sequence[str]]],
) -> list[Finding]:
    """Warn once for each LCSC number assigned to parts with different values.

    ``groups`` maps each such number to its values, and each value to the
    references that carry it.
    """
    findings = []
    for lcsc, values in groups.items():
        listed = "; ".join(
            f"{value} on {', '.join(references)}"
            for value, references in values.items()
        )
        # The title is cut off in the pane when a value has many parts, so the
        # detail lists each value on its own line.
        by_value = "\n".join(
            f"{value}: {', '.join(references)}" for value, references in values.items()
        )
        findings.append(
            Finding(
                Severity.WARNING,
                "Part values",
                f"{lcsc}: {listed}",
                f"These parts share LCSC number {lcsc} but have different values, "
                "so at least one of them is probably assigned the wrong part."
                f"\n\n{by_value}",
                tuple(ref for references in values.values() for ref in references),
            )
        )
    return findings


def placeholder_findings(count: int) -> list[Finding]:
    """Warn unless the silkscreen holds exactly one order-number placeholder."""
    if count == 1:
        return []
    if count == 0:
        title = "No order-number placeholder (JLCJLCJLCJLC) on silkscreen"
    else:
        title = f"{count} order-number placeholders on silkscreen"
    return [Finding(Severity.WARNING, "Order number", title, _PLACEHOLDER_DETAIL)]


def empty_zone_findings(pours: Sequence[str]) -> list[Finding]:
    """Flag each copper zone layer that holds no filled copper.

    Each entry names a zone's net and layer, such as "GND on B.Cu".
    """
    return [
        Finding(
            Severity.ERROR,
            "Copper zones",
            f"Copper zone {pour} has no fill",
            "This zone contains no filled copper. "
            "Plotting now ships the board without that copper.",
        )
        for pour in pours
    ]


def drc_findings(count: int, messages: Sequence[str]) -> list[Finding]:
    """Report DRC's error violations as one finding that lists their messages."""
    if count <= 0:
        return []
    noun = "violation" if count == 1 else "violations"
    detail = "Resolve or exclude DRC errors before manufacturing whenever possible."
    if messages:
        detail += "\n\n" + "\n".join(messages)
    return [Finding(Severity.ERROR, "DRC", f"DRC found {count} error {noun}", detail)]


def drc_failed_finding(reason: str) -> Finding:
    """Report that the forced DRC could not run, so the board went unchecked."""
    lines = reason.strip().splitlines()
    return Finding(
        Severity.ERROR,
        "DRC",
        f"DRC couldn't run: {lines[0]}" if lines else "DRC couldn't run",
        "The forced DRC check did not run, so this board has not been checked."
        + (f"\n\n{reason.strip()}" if lines else ""),
    )


def needs_review(findings: Sequence[Finding]) -> bool:
    """Whether any finding needs the user's decision; info findings never do."""
    return any(
        finding.severity in (Severity.ERROR, Severity.WARNING) for finding in findings
    )


def log_findings(logger: logging.Logger, findings: Sequence[Finding]) -> None:
    """Log info findings at INFO and the others at WARNING, in check order."""
    for finding in findings:
        log = logger.info if finding.severity == Severity.INFO else logger.warning
        log("%s: %s", finding.check, finding.title)


def group_findings(
    findings: Sequence[Finding],
) -> list[tuple[Severity, list[Finding]]]:
    """Group findings by severity, most severe first, each group in check order."""
    groups = []
    for severity in Severity:
        group = [finding for finding in findings if finding.severity == severity]
        if group:
            groups.append((severity, group))
    return groups


def summary_text(findings: Sequence[Finding]) -> str:
    """Count the findings by severity, e.g. "2 serious issues, 1 info"."""
    counts = []
    for severity, group in group_findings(findings):
        plural = "" if len(group) == 1 else "s"
        if severity == Severity.ERROR:
            counts.append(f"{len(group)} serious issue{plural}")
        elif severity == Severity.WARNING:
            counts.append(f"{len(group)} warning{plural}")
        else:
            counts.append(f"{len(group)} info")
    return ", ".join(counts)


def detail_text(finding: Finding) -> str:
    """Describe a selected finding: its detail, then the parts it names."""
    sections = [finding.detail] if finding.detail else []
    if finding.references:
        sections.append("Parts: " + ", ".join(finding.references))
    return "\n\n".join(sections) or finding.title
