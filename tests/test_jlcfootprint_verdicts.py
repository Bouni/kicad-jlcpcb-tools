"""Tests for the per-project verdict store: round trips, overrides, emitted rotation, display, origin."""

from contextlib import closing
import sqlite3

import pytest

from jlcfootprint.fit import package_origin
from jlcfootprint.geometry import Pad, pad_hash
from jlcfootprint.resolver import Verdict
from jlcfootprint.verdicts import PENDING, StoredVerdict, VerdictStore, verdict_key

from .test_jlcfootprint_origin import placement

VERDICT_COLUMNS = [
    "lcsc",
    "footprint_hash",
    "footprint_name",
    "puuid",
    "status",
    "fit",
    "rotation",
    "method",
    "confidence",
    "name_rotation",
    "polarity_light",
    "pad_count_kicad",
    "pad_count_jlc",
    "overlap_min",
    "overlap_mean",
    "angular_rms",
    "residual_mm",
    "body_excess_mm",
    "origin_dx_mm",
    "origin_dy_mm",
    "override_rotation",
    "override_note",
    "notes",
    "resolved_at",
]


@pytest.fixture
def store(tmp_path):
    """Return a verdict store in a fresh project database."""
    return VerdictStore(str(tmp_path / "project.db"))


def _verdict(**overrides) -> Verdict:
    verdict = Verdict(
        status="green",
        fit="fits",
        rotation=180,
        method="geometry",
        confidence="high",
        name_rotation=180,
        pad_count_kicad=3,
        pad_count_jlc=3,
        matched_pads=3,
        overlap_min=0.9,
        overlap_mean=0.95,
        angular_rms=0.1,
        residual_mm=0.01,
        notes=["hello"],
    )
    for key, value in overrides.items():
        setattr(verdict, key, value)
    return verdict


def test_the_table_has_exactly_these_columns_keyed_on_lcsc_and_hash(store):
    """The table has exactly these columns, in this order, keyed on (lcsc, footprint_hash)."""
    with closing(sqlite3.connect(store.db_path)) as con:
        columns = [
            row[1] for row in con.execute("PRAGMA table_info(footprint_verdict)")
        ]
        keys = [
            row[1]
            for row in con.execute("PRAGMA table_info(footprint_verdict)")
            if row[5]
        ]
    assert columns == VERDICT_COLUMNS
    assert keys == ["lcsc", "footprint_hash"]
    VerdictStore(store.db_path)  # opening it again changes nothing


def test_save_and_get_round_trip(store):
    """Every verdict field lands in its column and comes back."""
    stored = store.save("C2132", "abc", "Package:SOT-23", "puuid", _verdict(), now=42)
    assert store.get("C2132", "abc") == stored
    assert stored.footprint_name == "Package:SOT-23"
    assert stored.puuid == "puuid"
    assert (stored.status, stored.fit, stored.rotation, stored.method) == (
        "green",
        "fits",
        180,
        "geometry",
    )
    assert (stored.confidence, stored.name_rotation, stored.polarity_light) == (
        "high",
        180,
        None,
    )
    assert (stored.pad_count_kicad, stored.pad_count_jlc) == (3, 3)
    assert (
        stored.overlap_min,
        stored.overlap_mean,
        stored.angular_rms,
        stored.residual_mm,
    ) == (0.9, 0.95, 0.1, 0.01)
    assert stored.notes == "hello"
    assert stored.resolved_at == 42
    assert store.get("C2132", "other") is None
    assert [v.footprint_hash for v in store.all()] == ["abc"]


def test_override_survives_resolving_and_pending(store):
    """A user's rotation stays on the row through re-resolution and a pending mark."""
    store.save("C1", "h", "fp", "p", _verdict(), now=1)
    store.set_override("C1", "h", 90, "checked in the preview")
    stored = store.save("C1", "h", "fp", "p", _verdict(rotation=270), now=2)
    assert (stored.rotation, stored.override_rotation, stored.override_note) == (
        270,
        90,
        "checked in the preview",
    )
    store.mark_pending("C1", "h", "fp", now=3)
    pending = store.get("C1", "h")
    assert pending.status == PENDING
    assert pending.rotation is None
    assert pending.override_rotation == 90
    store.set_override("C1", "h", None)
    assert store.get("C1", "h").override_rotation is None
    store.delete("C1", "h")
    assert store.get("C1", "h") is None


def test_mark_pending_creates_a_row(store):
    """A part waiting for its data has a pending row with its footprint name."""
    store.mark_pending("C1", "h", "fp", now=3)
    stored = store.get("C1", "h")
    assert (stored.status, stored.footprint_name, stored.resolved_at) == (
        PENDING,
        "fp",
        3,
    )
    assert stored.source == "raw"
    assert stored.emitted_rotation is None
    assert stored.display_text == "raw"  # what the CPL emits while the part waits


@pytest.mark.parametrize(
    ("status", "rotation", "override", "source", "emitted", "text"),
    [
        ("green", 180, None, "derived", 180, "180°"),
        ("yellow", 90, None, "derived", 90, "90° !"),
        ("red", 90, None, "raw", None, "raw"),
        ("unknown", None, None, "raw", None, "raw"),
        ("red", None, 270, "override", 270, "270° set"),
        ("green", 0, 180, "override", 180, "180° set"),
    ],
)
def test_emitted_rotation_precedence_and_display(
    status, rotation, override, source, emitted, text
):
    """Override first, then a green or yellow derived value, else the raw angle."""
    stored = StoredVerdict(
        "C1", "h", status=status, rotation=rotation, override_rotation=override
    )
    assert stored.source == source
    assert stored.emitted_rotation == emitted
    assert stored.display_text == text


def green(**overrides) -> Verdict:
    """Return a green verdict carrying a placement, with fields overridden."""
    verdict = Verdict(
        status="green", fit="fits", rotation=90, placement=placement(0, 0.4, -0.2)
    )
    for name, value in overrides.items():
        setattr(verdict, name, value)
    return verdict


def test_saving_a_fitting_verdict_writes_the_origin_and_a_refusal_writes_null(store):
    """Green and yellow rows carry the origin; red, unknown and pending rows do not."""
    fitting = store.save("C1", "h", "F", "p", green())
    assert (fitting.origin_dx_mm, fitting.origin_dy_mm) == pytest.approx(
        package_origin(placement(0, 0.4, -0.2))
    )
    assert fitting.origin == (fitting.origin_dx_mm, fitting.origin_dy_mm)

    refused = store.save("C1", "h", "F", "p", green(status="red", rotation=None))
    assert (refused.origin_dx_mm, refused.origin_dy_mm, refused.origin) == (
        None,
        None,
        None,
    )


def test_marking_a_row_pending_keeps_the_columns_but_reports_no_origin(store):
    """The columns survive a re-fetch, and the pending status keeps them out of the CPL."""
    store.save("C1", "h", "F", "p", green())

    store.mark_pending("C1", "h", "F")

    pending = store.get("C1", "h")
    assert (pending.origin_dx_mm, pending.origin_dy_mm) == pytest.approx(
        package_origin(placement(0, 0.4, -0.2))
    )
    assert pending.origin is None


def test_an_override_keeps_the_origin_because_it_only_changes_the_rotation(store):
    """Setting and clearing an override leaves the two columns exactly as they were."""
    saved = store.save("C1", "h", "F", "p", green())

    store.set_override("C1", "h", 270, "by hand")
    overridden = store.get("C1", "h")
    store.set_override("C1", "h", None)

    assert overridden.origin == saved.origin
    assert overridden.emitted_rotation == 270
    assert store.get("C1", "h").origin == saved.origin


def test_verdict_key_includes_the_pin_functions():
    """Swapped K/A functions give different keys; no function gives the pad hash itself."""
    cathode_first = [Pad("1", -1, 0, 1, 0.5, 0, "K"), Pad("2", 1, 0, 1, 0.5, 0, "A")]
    anode_first = [Pad("1", -1, 0, 1, 0.5, 0, "A"), Pad("2", 1, 0, 1, 0.5, 0, "K")]
    plain = [Pad("1", -1, 0, 1, 0.5, 0), Pad("2", 1, 0, 1, 0.5, 0)]
    assert verdict_key(cathode_first) != verdict_key(anode_first)
    assert verdict_key(plain) == pad_hash(plain)
    assert verdict_key(cathode_first) != verdict_key(plain)
    spelled = [
        Pad("1", -1, 0, 1, 0.5, 0, "k_1"),
        Pad("2", 1, 0, 1, 0.5, 0, "anode"),
    ]
    assert verdict_key(spelled) != verdict_key(cathode_first)
    assert verdict_key(
        [Pad("1", -1, 0, 1, 0.5, 0, "K"), Pad("2", 1, 0, 1, 0.5, 0, "A")]
    ) == verdict_key([Pad("2", 1, 0, 1, 0.5, 0, "a"), Pad("1", -1, 0, 1, 0.5, 0, "k")])


def test_swapped_numbered_signs_are_stored_under_different_keys():
    """The key reads ``+_1`` as ``+``: swapped signs on the same pads need opposite rotations.

    Swapped K/A functions get different keys because the corner-case board's D4
    and D5 share one LCSC on one footprint with their functions swapped; numbered
    signs get the same protection.  A battery with ``+`` on pad 1 and one with
    ``+`` on pad 2 must not share a stored verdict, and neither may share the key
    of the bare pads.
    """
    plus_first = [
        Pad("1", -1, 0, 1, 0.5, 0, "+_1"),
        Pad("2", 1, 0, 1, 0.5, 0, "-_2"),
    ]
    minus_first = [
        Pad("1", -1, 0, 1, 0.5, 0, "-_1"),
        Pad("2", 1, 0, 1, 0.5, 0, "+_2"),
    ]
    assert verdict_key(plus_first) != verdict_key(minus_first)
    assert verdict_key(plus_first) != pad_hash(plus_first)


def test_the_verdict_key_follows_the_reading_of_a_numbered_sign():
    """The key stores what ``+_1`` reads as, so it equals the key of a bare ``+``."""
    numbered = [
        Pad("1", -1, 0, 1, 0.5, 0, "+_1"),
        Pad("2", 1, 0, 1, 0.5, 0, "-_2"),
    ]
    bare = [pad._replace(pin_function=pad.pin_function[0]) for pad in numbered]
    assert verdict_key(numbered) == verdict_key(bare)


def test_the_verdict_key_reads_each_function_whole():
    """Functions that share a leading word, or start with a sign, still key apart.

    The resolver pairs pads by their whole signal names, so ``DATA_IN`` and
    ``DATA_OUT`` swapped between two pads can give another verdict.
    """

    def keyed(first, second):
        return verdict_key(
            [
                Pad("1", -1, 0, 1, 0.5, 0, first),
                Pad("2", 1, 0, 1, 0.5, 0, second),
                Pad("3", 0, 1, 1, 0.5, 0, "GND"),
            ]
        )

    assert keyed("DATA_IN", "DATA_OUT") != keyed("DATA_OUT", "DATA_IN")
    assert keyed("IN+", "IN-") != keyed("IN-", "IN+")
    assert keyed("+5V", "-5V") != keyed("-5V", "+5V")
    assert keyed("IN+_1", "IN-_2") == keyed("IN+", "IN-")
    # A sign numbered for another pin still reads as the sign for polarity.
    assert keyed("+_2", "") != keyed("", "")
    assert keyed("+_2", "") != keyed("", "+_2")
    # A name without letters names a pin: beside K it is no diode.
    assert keyed("K", "1") != keyed("K", "")
    assert keyed("K", "NC") != keyed("K", "1")
