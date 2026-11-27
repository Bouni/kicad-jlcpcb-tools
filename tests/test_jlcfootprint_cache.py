"""Tests for the global EasyEDA cache: round trips, retry rules, the layout version."""

from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from jlcfootprint.boardfile import footprint_pads, parse_kicad_pcb
from jlcfootprint.cache import NONE_RETRY_S, SCHEMA_VERSION, Cache
from jlcfootprint.easyeda_parse import (
    ComponentRecord,
    FootprintRecord,
    SymbolPin,
    SymbolRecord,
    parse_puuid_response,
    parse_symbol_response,
)
from jlcfootprint.geometry import mirror_box, mirror_y
from jlcfootprint.resolving import resolve_record

from .jlcfootprint_support import pro_hit, pro_record, recorded, recorded_document

CORNER_CASE = (
    Path(__file__).parent
    / "fixtures"
    / "jlcfootprint"
    / "boards"
    / "corner_case"
    / "corner_case.kicad_pcb"
)


BOARDS = CORNER_CASE.parent.parent


def gate_footprints(board):
    """Yield a gate board's footprints with an LCSC field, pads and courtyard in the footprint frame."""
    for fp in parse_kicad_pcb(str(BOARDS / board)):
        if not fp.lcsc:
            continue
        pads, courtyard = footprint_pads(fp), fp.courtyard
        if fp.is_bottom:
            pads = mirror_y(pads)
            courtyard = None if courtyard is None else mirror_box(courtyard)
        yield fp, pads, courtyard


GATE_ANSWERS = [
    ("corner_case/corner_case.kicad_pcb", recorded),
    ("corner_case/corner_case.kicad_pcb", pro_record),
    ("pin_functions_and_marks/pin_functions_and_marks.kicad_pcb", pro_record),
]


def _rounded(pads):
    return [
        (
            p["number"],
            round(p["x"], 4),
            round(p["y"], 4),
            round(p["w"], 4),
            round(p["h"], 4),
        )
        for p in pads
    ]


@pytest.fixture
def cache(tmp_path):
    """Return a fresh cache file."""
    return Cache(str(tmp_path / "jlcfootprint-cache.db"))


def test_schema_and_version(cache):
    """Both tables exist and the version is recorded once."""
    with closing(sqlite3.connect(cache.path)) as con:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = con.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert {"meta", "lcsc_map", "package"} <= tables
    assert version == SCHEMA_VERSION
    Cache(cache.path)  # reopening keeps the version and never fails
    assert cache.counts() == {"parts": 0, "packages": 0}


def test_live_record_round_trip(cache):
    """A fetched part comes back with its symbol, its footprint and both drawings."""
    record = recorded("C2132")
    cache.store(record, now=1000)
    part = cache.part("C2132")
    assert part is not None
    assert part.fetched_at == 1000
    got = part.record
    assert got.status == "ok"
    assert got.puuid == record.puuid
    assert got.package_name == record.package_name
    assert got.symbol_uuid == record.symbol_uuid
    assert got.symbol_pins == record.symbol_pins
    assert _rounded(got.pads) == _rounded(record.pads)
    assert got.symbol_shapes == record.symbol_shapes
    assert got.footprint_shapes == record.footprint_shapes
    assert got.footprint_source == "component"
    assert got.footprint_origin == record.footprint_origin != (0.0, 0.0)
    assert cache.has_package(record.puuid)
    assert cache.counts() == {"parts": 1, "packages": 1}


def test_a_cached_classic_part_resolves_as_its_record_does(cache):
    """The drawing's origin comes back with it, so the body outline is read where it is drawn.

    Q2 on the corner-case board is C2132, a SOT-23 whose classic drawing has its
    origin at (4000.0005, 3000) canvas units: read against (0, 0) instead, its body
    would sit a metre away and overhang the courtyard by that much.
    """
    record = recorded("C2132")
    q2 = next(fp for fp in parse_kicad_pcb(str(CORNER_CASE)) if fp.reference == "Q2")
    pads = footprint_pads(q2)
    direct = resolve_record(pads, q2.footprint_name, record, q2.courtyard)
    cache.store(record, now=1)
    cached = resolve_record(
        pads, q2.footprint_name, cache.part("C2132").record, q2.courtyard
    )
    assert direct.body_excess_mm is None and cached.body_excess_mm is None
    assert (cached.status, cached.rotation, cached.fit, cached.origin) == (
        direct.status,
        direct.rotation,
        direct.fit,
        direct.origin,
    )
    assert cached.note_text == direct.note_text


def test_needs_rules(cache):
    """Absent and error rows fetch; ok rows never; none rows after thirty days."""
    assert cache.needs("C1")
    cache.store(recorded("C2132"), now=1000)
    assert not cache.needs("C2132", now=10**9)
    cache.store(ComponentRecord(lcsc="C2", status="error", error="HTTP 403"), now=1000)
    assert cache.needs("C2", now=1001)
    cache.store(ComponentRecord(lcsc="C3", status="none"), now=1000)
    assert not cache.needs("C3", now=1000 + NONE_RETRY_S - 1)
    assert cache.needs("C3", now=1000 + NONE_RETRY_S)
    assert cache.status("C3") == "none"
    assert cache.status("C4") is None


def test_error_and_none_rows_carry_no_footprint(cache):
    """A part EasyEDA has nothing for stores its status and nothing else."""
    cache.store(ComponentRecord(lcsc="C3", status="none"), now=5)
    part = cache.part("C3")
    assert part.record.status == "none"
    assert part.record.pads == []
    assert part.record.symbol_pins == []


def test_store_footprint_from_the_uuid_endpoint(cache):
    """A footprint fetched by uuid gets its own package row; an empty one is ignored."""
    record = recorded("C2132")
    cache.store(ComponentRecord(lcsc="C2132", status="ok", puuid=record.puuid), now=1)
    assert cache.part("C2132").record.pads == []
    cache.store_footprint(FootprintRecord(puuid=record.puuid, status="error"), now=2)
    assert not cache.has_package(record.puuid)
    cache.store_footprint(
        FootprintRecord(
            puuid=record.puuid,
            status="ok",
            package_name=record.package_name,
            pads=record.pads,
            footprint_shapes=record.footprint_shapes,
        ),
        now=2,
    )
    part = cache.part("C2132")
    assert _rounded(part.record.pads) == _rounded(record.pads)
    assert part.record.footprint_source == "puuid-endpoint"
    assert part.record.footprint_origin == (0.0, 0.0)
    cache.store_footprint(
        FootprintRecord(
            puuid=record.puuid,
            status="ok",
            package_name=record.package_name,
            pads=record.pads,
            footprint_shapes=record.footprint_shapes,
            footprint_origin=record.footprint_origin,
        ),
        now=3,
    )
    assert cache.part("C2132").record.footprint_origin == record.footprint_origin


def test_forget_and_clear(cache):
    """Forgetting a part keeps its footprint; clearing drops everything."""
    cache.store(recorded("C2132"), now=1)
    cache.forget("C2132")
    assert cache.part("C2132") is None
    assert cache.counts() == {"parts": 0, "packages": 1}
    cache.store(recorded("C2132"), now=1)
    cache.clear()
    assert cache.counts() == {"parts": 0, "packages": 0}


def test_needs_follows_a_live_row_through_its_pieces(cache):
    """No row asks for a lookup; the uuids ask for their documents; a complete row asks for nothing."""
    assert cache.needs("C1") == {"lookup"}
    cache.store_lookup("C1", "sym-1", "fp-1", now=10)
    assert cache.needs("C1") == {"footprint", "symbol"}
    assert cache.needs("C1")
    part = cache.part("C1")
    assert (part.record.status, part.record.puuid, part.record.symbol_uuid) == (
        "ok",
        "fp-1",
        "sym-1",
    )
    assert part.record.symbol_pins == []
    record = recorded("C2132")
    cache.store_footprint(
        FootprintRecord(
            puuid="fp-1",
            status="ok",
            package_name=record.package_name,
            pads=record.pads,
        ),
        now=11,
    )
    assert cache.needs("C1") == {"symbol"}
    symbol = SymbolRecord(
        uuid="sym-1",
        status="ok",
        pins=[SymbolPin("1", "K"), SymbolPin("2", "A")],
        shapes=['["DOCTYPE","SYMBOL","1.1"]'],
    )
    assert cache.store_symbol("C1", symbol, now=12)
    assert cache.needs("C1") == set()
    part = cache.part("C1")
    assert part.record.symbol_pins == symbol.pins
    assert part.record.symbol_shapes == symbol.shapes
    assert part.record.package_name == record.package_name
    assert part.fetched_at == 12
    assert not cache.store_symbol("C9", symbol, now=12)
    # A part whose lookup named no symbol is complete once its footprint is in.
    cache.store_lookup("C2", "", "fp-1", now=10)
    assert cache.needs("C2") == set()
    assert cache.part("C2").record.symbol_pins == []
    # A shared footprint already cached is not asked for again.
    cache.store_lookup("C3", "sym-3", "fp-1", now=10)
    assert cache.needs("C3") == {"symbol"}


def test_lookup_misses_and_empty_symbols(cache):
    """A miss is a none row on the 30-day clock; a symbol that answered nothing leaves no pins."""
    cache.store_lookup_miss("C5", now=100)
    assert cache.status("C5") == "none"
    assert cache.needs("C5", now=100) == set()
    assert cache.needs("C5", now=100 + NONE_RETRY_S) == {"lookup"}
    cache.store_lookup("C6", "sym-6", "fp-6", now=1)
    assert cache.store_symbol("C6", SymbolRecord(uuid="sym-6", status="none"), now=2)
    assert cache.needs("C6") == {"footprint"}
    part = cache.part("C6")
    assert part.record.symbol_pins == []
    assert part.record.symbol_uuid == "sym-6"


def test_a_failed_symbol_is_not_stored_as_a_part_without_pins(cache):
    """A symbol fetch that failed leaves the row waiting for its symbol, so it is asked for again."""
    cache.store_lookup("C7", "sym-7", "", now=1)
    assert cache.needs("C7") == {"symbol"}
    failed = SymbolRecord(uuid="sym-7", status="error", error="HTTP 500 after retries")
    assert not cache.store_symbol("C7", failed, now=2)
    assert cache.needs("C7") == {"symbol"}
    assert cache.part("C7").fetched_at == 1


def test_a_late_symbol_answer_does_not_overwrite_a_newer_lookup(cache):
    """A part looked up again while its old symbol was in flight keeps waiting for the new one."""
    cache.store_lookup("C9", "sym-old", "", now=1)
    cache.store_lookup("C9", "sym-new", "", now=2)
    late = SymbolRecord(uuid="sym-old", status="ok", pins=[SymbolPin("1", "K")])
    assert not cache.store_symbol("C9", late, now=3)
    assert cache.needs("C9") == {"symbol"}
    assert cache.part("C9").record.symbol_uuid == "sym-new"
    cache.store(ComponentRecord(lcsc="C9", status="error", error="x"), now=4)
    assert not cache.store_symbol(
        "C9", SymbolRecord(uuid="sym-new", status="ok"), now=5
    ), "an error row takes no symbol"


def test_a_symbol_easyeda_served_unreadable_is_stored_without_pins(cache):
    """The same answer would come again, so the part is judged without the symbol."""
    cache.store_lookup("C8", "sym-8", "", now=1)
    unreadable = SymbolRecord(
        uuid="sym-8", status="error", error="no readable pins in the symbol"
    )
    assert cache.store_symbol("C8", unreadable, now=2, unreadable=True)
    assert cache.needs("C8") == set()
    assert cache.part("C8").record.symbol_pins == []


@pytest.mark.parametrize("version", ["1", "99"], ids=["older", "newer"])
def test_a_file_from_another_layout_is_dropped_and_refilled(tmp_path, version):
    """The cache is only fetched data, so a file of another layout starts over instead of failing on its columns."""
    path = str(tmp_path / "jlcfootprint-cache.db")
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            f"INSERT INTO meta VALUES ('schema_version', '{version}');"
            "CREATE TABLE lcsc_map (lcsc TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " source TEXT NOT NULL, fetched_at INTEGER NOT NULL);"
            "INSERT INTO lcsc_map VALUES ('C9', 'ok', 'live', 1);"
            "CREATE TABLE package (puuid TEXT PRIMARY KEY, package_name TEXT NOT NULL,"
            " pads_json TEXT NOT NULL, source TEXT NOT NULL, fetched_at INTEGER NOT NULL);"
            "INSERT INTO package VALUES ('fp-9', 'R0603', '[]', 'live', 1);"
        )
    cache = Cache(path)
    assert cache.counts() == {"parts": 0, "packages": 0}
    cache.store(recorded("C2132"), now=1)
    assert cache.part("C2132").record.status == "ok"
    with closing(sqlite3.connect(path)) as con:
        assert con.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == (SCHEMA_VERSION,)
    # Reopened at its own layout, the file keeps what it holds.
    assert Cache(path).counts()["parts"] == 1


def test_a_second_process_opening_during_a_rebuild_waits_and_keeps_it(
    tmp_path, monkeypatch
):
    """Two processes open a file of another layout: the second waits for the first's rebuild, then finds the new layout."""
    path = str(tmp_path / "jlcfootprint-cache.db")
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO meta VALUES ('schema_version', '1');"
        )
    second: dict = {}

    def open_second() -> None:
        try:
            second["cache"] = Cache(path)
        except Exception as error:
            second["error"] = error

    thread = threading.Thread(target=open_second)
    rebuild = Cache._create_or_rebuild
    seen = []

    def first_lets_the_second_in(con) -> None:
        seen.append(
            con.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        if len(seen) == 1:
            # The first holds the write lock: the second starts now and must wait.
            thread.start()
            time.sleep(0.3)
        rebuild(con)

    monkeypatch.setattr(
        Cache, "_create_or_rebuild", staticmethod(first_lets_the_second_in)
    )
    first = Cache(path)
    first.store(recorded("C2132"), now=1)
    thread.join(timeout=10)
    monkeypatch.undo()
    assert "error" not in second
    # The second read the layout only after the first had committed the new one.
    assert seen == ["1", SCHEMA_VERSION]
    assert first.part("C2132").record.status == "ok"
    assert second["cache"].counts()["parts"] == 1


def test_the_lookup_package_name_stands_in_for_a_footprint_document_without_one(cache):
    """A footprint document with no title keeps the lookup's name; one with a title wins."""
    record = recorded("C2132")
    cache.store_lookup("C1", "", "fp-1", now=10, package_name=record.package_name)
    assert cache.part("C1").record.package_name == record.package_name
    cache.store_footprint(
        FootprintRecord(puuid="fp-1", status="ok", package_name="", pads=record.pads),
        now=11,
    )
    assert cache.part("C1").record.package_name == record.package_name
    cache.store_footprint(
        FootprintRecord(
            puuid="fp-1", status="ok", package_name="SOT-23-3_X", pads=record.pads
        ),
        now=12,
    )
    assert cache.part("C1").record.package_name == "SOT-23-3_X"


@pytest.mark.parametrize(
    ("board", "answer"), GATE_ANSWERS, ids=["classic", "pro", "second-board"]
)
def test_every_gate_part_resolves_from_the_cache_as_from_its_record(
    tmp_path, board, answer
):
    """Stored whole, every gate-board part reads back to the same verdict in every field."""
    cache = Cache(str(tmp_path / "jlcfootprint-cache.db"))
    checked = 0
    for fp, pads, courtyard in gate_footprints(board):
        record = answer(fp.lcsc)
        cache.store(record, now=1)
        direct = resolve_record(pads, fp.footprint_name, record, courtyard)
        cached = resolve_record(
            pads, fp.footprint_name, cache.part(fp.lcsc).record, courtyard
        )
        assert asdict(cached) == asdict(direct), fp.reference
        checked += 1
    assert checked == (16 if board.startswith("pin") else 47)


@pytest.mark.parametrize(
    "board", [board for board, answer in GATE_ANSWERS if answer is pro_record]
)
def test_every_pro_part_filled_in_steps_resolves_as_its_record_does(tmp_path, board):
    """The lookup, then the footprint document, then the symbol: the steps a fetch takes."""
    cache = Cache(str(tmp_path / "jlcfootprint-cache.db"))
    for fp, pads, courtyard in gate_footprints(board):
        hit = pro_hit(fp.lcsc)
        cache.store_lookup(
            fp.lcsc, hit.symbol_uuid, hit.puuid, now=1, package_name=hit.package_name
        )
        cache.store_footprint(
            parse_puuid_response(recorded_document("footprint", hit.puuid), hit.puuid),
            now=2,
        )
        if hit.symbol_uuid:
            symbol = parse_symbol_response(
                recorded_document("symbol", hit.symbol_uuid), hit.symbol_uuid
            )
            assert cache.store_symbol(fp.lcsc, symbol, now=3)
        assert cache.needs(fp.lcsc) == set(), fp.reference
        direct = resolve_record(pads, fp.footprint_name, pro_record(fp.lcsc), courtyard)
        cached = resolve_record(
            pads, fp.footprint_name, cache.part(fp.lcsc).record, courtyard
        )
        assert asdict(cached) == asdict(direct), fp.reference
