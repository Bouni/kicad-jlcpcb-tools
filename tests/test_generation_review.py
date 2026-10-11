"""Generation checks become findings, worded and grouped for the review pane."""

from collections.abc import Iterator
import logging
from types import ModuleType

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def review() -> Iterator[ModuleType]:
    """Load the real findings module as a plugin sibling."""
    with load_siblings("generation_review_tests", ("generation_review",), {}) as loaded:
        yield loaded["generation_review"]


def _finding(review: ModuleType, severity: str, title: str) -> object:
    """Build a finding of the named severity from a check called after it."""
    return review.Finding(review.Severity[severity], severity.title(), title)


def test_value_mismatch_names_every_reference_of_each_number(
    review: ModuleType,
) -> None:
    """One warning per LCSC number, naming each value and the parts carrying it."""
    findings = review.value_mismatch_findings(
        {
            "C14663": {"100n": ["C3", "C4"], "0.1uF": ["C7"]},
            "C25804": {"10k": ["R1"], "10K": ["R2"]},
        }
    )

    assert [(f.severity, f.check, f.title, f.references) for f in findings] == [
        (
            review.Severity.WARNING,
            "Part values",
            "C14663: 100n on C3, C4; 0.1uF on C7",
            ("C3", "C4", "C7"),
        ),
        (
            review.Severity.WARNING,
            "Part values",
            "C25804: 10k on R1; 10K on R2",
            ("R1", "R2"),
        ),
    ]
    assert "C14663" in findings[0].detail
    assert review.value_mismatch_findings({}) == []


def test_detail_lists_each_value_with_its_parts(review: ModuleType) -> None:
    """The odd part out stays findable when the row's title is cut off."""
    references = [f"C{index}" for index in range(1, 31)]
    (finding,) = review.value_mismatch_findings(
        {"C1525": {"100nF": references, "0.1uF": ["C31", "C32"], "100n": ["C40"]}}
    )

    assert finding.detail.endswith(
        "\n\n100nF: " + ", ".join(references) + "\n0.1uF: C31, C32\n100n: C40"
    )


def test_hundreds_of_references_stay_on_one_row_and_reach_the_detail(
    review: ModuleType,
) -> None:
    """A large group keeps a one-line title while its detail lists every part."""
    references = [f"C{index}" for index in range(1, 401)]
    (finding,) = review.value_mismatch_findings(
        {"C1525": {"100n": references[:-1], "0.1uF": references[-1:]}}
    )

    assert "\n" not in finding.title
    assert finding.references == tuple(references)
    assert review.detail_text(finding).endswith("Parts: " + ", ".join(references))


@pytest.mark.parametrize(
    ("count", "title"),
    [
        (0, "No order-number placeholder (JLCJLCJLCJLC) on silkscreen"),
        (1, None),
        (3, "3 order-number placeholders on silkscreen"),
    ],
)
def test_placeholder_warns_unless_there_is_exactly_one(
    review: ModuleType, count: int, title: str
) -> None:
    """None and several placeholders are warnings; exactly one is clean."""
    findings = review.placeholder_findings(count)

    if title is None:
        assert findings == []
    else:
        assert [(f.severity, f.check, f.title) for f in findings] == [
            (review.Severity.WARNING, "Order number", title)
        ]
        assert "JLCJLCJLCJLC" in findings[0].detail


def test_each_empty_zone_layer_is_an_error_with_its_consequence(
    review: ModuleType,
) -> None:
    """Every empty pour is its own red row, in the order the check found them."""
    findings = review.empty_zone_findings(["GND on B.Cu", "no net on F.Cu"])

    assert [(f.severity, f.check, f.title) for f in findings] == [
        (review.Severity.ERROR, "Copper zones", "Copper zone GND on B.Cu has no fill"),
        (
            review.Severity.ERROR,
            "Copper zones",
            "Copper zone no net on F.Cu has no fill",
        ),
    ]
    assert all(
        "Plotting now ships the board without that copper." in f.detail
        for f in findings
    )
    assert review.empty_zone_findings([]) == []


def test_drc_errors_are_one_finding_listing_every_message(
    review: ModuleType,
) -> None:
    """Dozens of messages stay in the detail; the row keeps one line."""
    messages = [f"[clearance]: Clearance violation {index}" for index in range(40)]

    (finding,) = review.drc_findings(40, messages)

    assert (finding.severity, finding.check, finding.title) == (
        review.Severity.ERROR,
        "DRC",
        "DRC found 40 error violations",
    )
    assert finding.detail.splitlines()[-40:] == messages
    assert review.drc_findings(1, messages[:1])[0].title == (
        "DRC found 1 error violation"
    )
    assert review.drc_findings(0, []) == []


def test_drc_count_without_messages_still_needs_review(review: ModuleType) -> None:
    """A report without per-item severity still reports its count."""
    (finding,) = review.drc_findings(4, [])

    assert finding.title == "DRC found 4 error violations"
    assert review.needs_review([finding])


@pytest.mark.parametrize(
    ("reason", "title"),
    [
        (
            "the board must be saved first",
            "DRC couldn't run: the board must be saved first",
        ),
        (
            "WriteDRCReport returned failure\nTraceback line",
            "DRC couldn't run: WriteDRCReport returned failure",
        ),
        ("", "DRC couldn't run"),
    ],
)
def test_drc_failure_is_an_error_with_a_one_line_title(
    review: ModuleType, reason: str, title: str
) -> None:
    """The reason's first line titles the row; all of it goes in the detail."""
    finding = review.drc_failed_finding(reason)

    assert (finding.severity, finding.check, finding.title) == (
        review.Severity.ERROR,
        "DRC",
        title,
    )
    assert reason in finding.detail
    assert "has not been checked" in finding.detail


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        ((), False),
        (("INFO", "INFO"), False),
        (("INFO", "WARNING"), True),
        (("ERROR",), True),
    ],
)
def test_only_errors_and_warnings_need_review(
    review: ModuleType, severities: tuple[str, ...], expected: bool
) -> None:
    """Info findings alone never open the pane."""
    findings = [_finding(review, severity, "row") for severity in severities]

    assert review.needs_review(findings) is expected


def test_log_puts_info_findings_at_info_and_the_rest_at_warning(
    review: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    """Findings log in check order, each at its own level."""
    logger = logging.getLogger("generation_review_tests.log")
    findings = [
        _finding(review, "INFO", "pin-1 mark differs"),
        _finding(review, "ERROR", "DRC found 2 error violations"),
        _finding(review, "WARNING", "No order-number placeholder"),
    ]

    with caplog.at_level(logging.INFO, logger=logger.name):
        review.log_findings(logger, findings)
        review.log_findings(logger, findings[:1])

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "Info: pin-1 mark differs"),
        (logging.WARNING, "Error: DRC found 2 error violations"),
        (logging.WARNING, "Warning: No order-number placeholder"),
        (logging.INFO, "Info: pin-1 mark differs"),
    ]


def test_groups_run_serious_warnings_info_and_keep_check_order(
    review: ModuleType,
) -> None:
    """Empty groups are left out; each group keeps the order checks ran in."""
    findings = [
        _finding(review, "WARNING", "w1"),
        _finding(review, "INFO", "n1"),
        _finding(review, "ERROR", "e1"),
        _finding(review, "WARNING", "w2"),
        _finding(review, "ERROR", "e2"),
    ]

    grouped = review.group_findings(findings)

    assert [(severity, [f.title for f in group]) for severity, group in grouped] == [
        (review.Severity.ERROR, ["e1", "e2"]),
        (review.Severity.WARNING, ["w1", "w2"]),
        (review.Severity.INFO, ["n1"]),
    ]
    assert [severity for severity, _ in review.group_findings(findings[:2])] == [
        review.Severity.WARNING,
        review.Severity.INFO,
    ]
    assert review.HEADINGS == {
        review.Severity.ERROR: "Serious issues",
        review.Severity.WARNING: "Warnings",
        review.Severity.INFO: "Info",
    }


@pytest.mark.parametrize(
    ("severities", "summary"),
    [
        (
            ("ERROR",) * 3 + ("WARNING",) * 3 + ("INFO",) * 2,
            "3 serious issues, 3 warnings, 2 info",
        ),
        (("WARNING", "INFO"), "1 warning, 1 info"),
        (("ERROR",), "1 serious issue"),
    ],
)
def test_summary_counts_each_severity_present(
    review: ModuleType, severities: tuple[str, ...], summary: str
) -> None:
    """The header names only the severities that have findings."""
    findings = [_finding(review, severity, "row") for severity in severities]

    assert review.summary_text(findings) == summary


def test_detail_text_shows_detail_then_parts(review: ModuleType) -> None:
    """Either part may be missing; a bare finding falls back to its title."""
    finding = review.Finding(
        review.Severity.WARNING, "Part values", "title", "detail", ("C1", "C2")
    )

    assert review.detail_text(finding) == "detail\n\nParts: C1, C2"
    assert (
        review.detail_text(
            review.Finding(review.Severity.WARNING, "Part values", "title", "", ("C1",))
        )
        == "Parts: C1"
    )
    assert (
        review.detail_text(review.Finding(review.Severity.INFO, "Info", "title"))
        == "title"
    )


def test_every_builder_title_is_one_line(review: ModuleType) -> None:
    """Multi-line inputs never make a list row taller than one line."""
    findings = [
        *review.value_mismatch_findings({"C1": {"1k": ["R1"], "1K": ["R2"]}}),
        *review.placeholder_findings(0),
        *review.placeholder_findings(2),
        *review.empty_zone_findings(["GND on F.Cu"]),
        *review.drc_findings(3, ["[a]: one", "[b]: two", "[c]: three"]),
        review.drc_failed_finding("first line\nsecond line"),
    ]

    assert all(f.title and "\n" not in f.title for f in findings)
