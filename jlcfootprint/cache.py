"""Global EasyEDA cache: one sqlite file, ``jlcfootprint-cache.db``, shared by every project.

Symbol data is keyed per LCSC because pin meaning is a per-part fact; footprint
data is keyed per footprint uuid because footprints are shared.  Pads are stored
in the parser's raw form (classic canvas units) and converted to millimetres when
a part is judged, so a change to the frame convention never invalidates the cache.
A classic drawing's own origin (its head x/y, which its pads have already had
subtracted) is stored with it, because the body outline and the marks in the
stored drawing are read against it; it is (0, 0) for Pro text.

No row expires on a timer; ``needs`` decides what is fetched again.  A ``none``
row (EasyEDA has nothing for the part) is looked up again after 30 days, an
``error`` row (a failed fetch) the next time the part is checked, and an ``ok``
row never, until ``forget`` or ``clear`` drops it.  A row is filled in steps: the
batch lookup writes the uuids, the footprint and the symbol documents follow, and
``needs`` says which piece is still missing.

A file written under another layout is dropped and refilled, since every row in
it can be fetched again.  Stdlib only; safe to use from the worker thread and the
main thread at once (each call opens its own connection).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import sqlite3
import time
from typing import Any, Optional
import zlib

from .easyeda_parse import ComponentRecord, FootprintRecord, SymbolPin, SymbolRecord

FILENAME = "jlcfootprint-cache.db"
SCHEMA_VERSION = "5"
NONE_RETRY_S = 30 * 24 * 3600
CONNECT_TIMEOUT_S = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS lcsc_map (
  lcsc             TEXT PRIMARY KEY,
  puuid            TEXT,
  status           TEXT NOT NULL,
  error            TEXT,
  symbol_uuid      TEXT,
  symbol_pins_json TEXT,
  symbol_blob      BLOB,
  package_name     TEXT,
  fetched_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS package (
  puuid            TEXT PRIMARY KEY,
  package_name     TEXT NOT NULL,
  pads_json        TEXT NOT NULL,
  footprint_blob   BLOB,
  footprint_source TEXT,
  origin_x         REAL NOT NULL,
  origin_y         REAL NOT NULL,
  fetched_at       INTEGER NOT NULL
);
"""
# The schema one statement at a time, for running inside an explicit transaction:
# executescript() would commit it first.
_SCHEMA_STATEMENTS = [statement for statement in SCHEMA.split(";") if statement.strip()]

_LCSC_COLUMNS = (
    "lcsc",
    "puuid",
    "status",
    "error",
    "symbol_uuid",
    "symbol_pins_json",
    "symbol_blob",
    "package_name",
    "fetched_at",
)
_PACKAGE_COLUMNS = (
    "puuid",
    "package_name",
    "pads_json",
    "footprint_blob",
    "footprint_source",
    "origin_x",
    "origin_y",
    "fetched_at",
)


@dataclass
class CachedPart:
    """One part as the cache knows it, ready for the resolver."""

    record: ComponentRecord
    fetched_at: int


def compress(shapes: list[str]) -> Optional[bytes]:
    """Return the drawing's shape strings as a zlib-compressed JSON blob, or None when empty."""
    if not shapes:
        return None
    return zlib.compress(json.dumps(shapes, separators=(",", ":")).encode("utf-8"))


def decompress(blob: Optional[bytes]) -> list[str]:
    """Return the shape strings stored by :func:`compress` (an empty list for None)."""
    if not blob:
        return []
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _upsert(con: sqlite3.Connection, table: str, columns: tuple, row: dict) -> None:
    """Insert or replace one row by primary key."""
    placeholders = ",".join("?" for _ in columns)
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        tuple(row.get(column) for column in columns),
    )


class Cache:
    """The global cache file; every method opens and closes its own connection."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        with closing(self.connect()) as con:
            # The layout check and any rebuild are one write transaction, taken
            # before the check: a second KiCad process opening the same file waits
            # for it, then finds the new layout, and never drops the tables the
            # first has just built.
            con.isolation_level = None
            con.execute("BEGIN IMMEDIATE")
            try:
                self._create_or_rebuild(con)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    @staticmethod
    def _create_or_rebuild(con: sqlite3.Connection) -> None:
        """Create the tables, or rebuild them when the file has another layout."""
        for statement in _SCHEMA_STATEMENTS:
            con.execute(statement)
        row = con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None and row[0] != SCHEMA_VERSION:
            # A file written under another layout, older or newer, is rebuilt
            # rather than migrated: every row in it was fetched and can be fetched
            # again, so a later change to these tables only has to raise
            # SCHEMA_VERSION.
            con.execute("DROP TABLE IF EXISTS lcsc_map")
            con.execute("DROP TABLE IF EXISTS package")
            con.execute("DELETE FROM meta")
            for statement in _SCHEMA_STATEMENTS:
                con.execute(statement)
        con.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )

    def connect(self) -> sqlite3.Connection:
        """Open a connection with row access by name."""
        con = sqlite3.connect(self.path, timeout=CONNECT_TIMEOUT_S)
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def status(self, lcsc: str) -> Optional[str]:
        """Return the stored status for a part, or None when it has no row."""
        with closing(self.connect()) as con:
            row = con.execute(
                "SELECT status FROM lcsc_map WHERE lcsc = ?", (lcsc,)
            ).fetchone()
        return None if row is None else str(row["status"])

    def needs(self, lcsc: str, now: Optional[float] = None) -> set[str]:
        """Return what the part still needs from the network.

        ``{'lookup'}`` when there is no row, an ``error`` row or a ``none`` row older
        than thirty days; nothing for a younger ``none`` row; otherwise the subset of
        ``{'footprint', 'symbol'}`` that is missing: a ``puuid`` without a package
        row, or a row whose symbol uuid is known but whose symbol has not been
        fetched.  An empty set means the part can be judged from the cache.
        """
        with closing(self.connect()) as con:
            row = con.execute(
                "SELECT status, fetched_at, puuid, symbol_uuid, symbol_pins_json"
                " FROM lcsc_map WHERE lcsc = ?",
                (lcsc,),
            ).fetchone()
            if row is None or row["status"] == "error":
                return {"lookup"}
            if row["status"] == "none":
                current = time.time() if now is None else now
                if current - float(row["fetched_at"]) >= NONE_RETRY_S:
                    return {"lookup"}
                return set()
            needed: set[str] = set()
            if row["puuid"] and not self._has_package(con, str(row["puuid"])):
                needed.add("footprint")
            if row["symbol_uuid"] and row["symbol_pins_json"] is None:
                needed.add("symbol")
        return needed

    @staticmethod
    def _has_package(con: sqlite3.Connection, puuid: str) -> bool:
        return (
            con.execute("SELECT 1 FROM package WHERE puuid = ?", (puuid,)).fetchone()
            is not None
        )

    def part(self, lcsc: str) -> Optional[CachedPart]:
        """Return the part's record with its footprint joined in, or None without a row."""
        with closing(self.connect()) as con:
            row = con.execute(
                "SELECT * FROM lcsc_map WHERE lcsc = ?", (lcsc,)
            ).fetchone()
            if row is None:
                return None
            package = None
            if row["puuid"]:
                package = con.execute(
                    "SELECT * FROM package WHERE puuid = ?", (row["puuid"],)
                ).fetchone()
        record = ComponentRecord(
            lcsc=lcsc,
            status=str(row["status"]),
            error=str(row["error"] or ""),
            symbol_uuid=str(row["symbol_uuid"] or ""),
            symbol_shapes=decompress(row["symbol_blob"]),
            puuid=str(row["puuid"] or ""),
            # The lookup's name, kept for a footprint document that carries none.
            package_name=str(row["package_name"] or ""),
        )
        if row["symbol_pins_json"]:
            record.symbol_pins = [
                SymbolPin(number=str(pin["number"]), label=str(pin.get("label", "")))
                for pin in json.loads(row["symbol_pins_json"])
            ]
        if package is not None:
            record.package_name = str(package["package_name"]) or record.package_name
            record.pads = json.loads(package["pads_json"])
            record.footprint_shapes = decompress(package["footprint_blob"])
            record.footprint_source = str(package["footprint_source"] or "")
            record.footprint_origin = (
                float(package["origin_x"]),
                float(package["origin_y"]),
            )
        return CachedPart(record=record, fetched_at=int(row["fetched_at"]))

    def has_package(self, puuid: str) -> bool:
        """Return True when a footprint with this uuid is stored."""
        with closing(self.connect()) as con:
            return self._has_package(con, puuid)

    def counts(self) -> dict[str, int]:
        """Return the row counts of both tables."""
        with closing(self.connect()) as con:
            parts = con.execute("SELECT COUNT(*) FROM lcsc_map").fetchone()[0]
            packages = con.execute("SELECT COUNT(*) FROM package").fetchone()[0]
        return {"parts": int(parts), "packages": int(packages)}

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def store(self, record: ComponentRecord, now: Optional[float] = None) -> None:
        """Store one fetched part: its symbol row and, when it carries pads, its footprint row.

        A record holds no pins both when its symbol answered ``none`` and when
        the symbol's fetch failed (``easyeda_parse.assemble_record`` keeps only an
        ``ok`` symbol's pins), so an ``ok`` record without pins leaves its symbol to
        be asked for again: ``store_symbol`` is what records a symbol that has
        none.
        """
        fetched_at = int(time.time() if now is None else now)
        row = {
            "lcsc": record.lcsc,
            "puuid": record.puuid or None,
            "status": record.status,
            "error": record.error or None,
            "symbol_uuid": record.symbol_uuid or None,
            "symbol_pins_json": (
                json.dumps(
                    [
                        {"number": pin.number, "label": pin.label}
                        for pin in record.symbol_pins
                    ],
                    separators=(",", ":"),
                )
                if record.symbol_pins
                else None
            ),
            "symbol_blob": compress(record.symbol_shapes),
            "package_name": record.package_name or None,
            "fetched_at": fetched_at,
        }
        with closing(self.connect()) as con, con:
            _upsert(con, "lcsc_map", _LCSC_COLUMNS, row)
            if record.puuid and record.pads:
                _upsert(
                    con,
                    "package",
                    _PACKAGE_COLUMNS,
                    self._package_row(
                        record.puuid,
                        record.package_name,
                        record.pads,
                        record.footprint_shapes,
                        record.footprint_source or "component",
                        record.footprint_origin,
                        fetched_at,
                    ),
                )

    def store_lookup(
        self,
        lcsc: str,
        symbol_uuid: str,
        puuid: str,
        now: Optional[float] = None,
        *,
        package_name: str = "",
    ) -> None:
        """Store what the batch lookup knows about a part: its uuids and package name, no drawings yet.

        A part with no symbol uuid gets an empty pin list, so ``needs`` never asks
        for a symbol that does not exist.  The package name stands in for the
        footprint document's own, should that document carry none, as
        ``easyeda_parse.assemble_record`` falls back to the lookup's.
        """
        fetched_at = int(time.time() if now is None else now)
        row = {
            "lcsc": lcsc,
            "puuid": puuid or None,
            "status": "ok",
            "error": None,
            "symbol_uuid": symbol_uuid or None,
            "symbol_pins_json": None if symbol_uuid else "[]",
            "symbol_blob": None,
            "package_name": package_name or None,
            "fetched_at": fetched_at,
        }
        with closing(self.connect()) as con, con:
            _upsert(con, "lcsc_map", _LCSC_COLUMNS, row)

    def store_lookup_miss(self, lcsc: str, now: Optional[float] = None) -> None:
        """Store that EasyEDA has nothing for a part: a ``none`` row retried after 30 days."""
        self.store(ComponentRecord(lcsc=lcsc, status="none"), now)

    def store_symbol(
        self,
        lcsc: str,
        symbol: SymbolRecord,
        now: Optional[float] = None,
        *,
        unreadable: bool = False,
    ) -> bool:
        """Store a part's fetched symbol on its row; return whether anything was stored.

        A symbol that answered ``none`` leaves an empty pin list: the part
        resolves by geometry and a polarized one reads as unknown.  So does one
        EasyEDA served but the parser could not read (``unreadable``, the
        client's ``Document.unreadable``): the same answer would come again.  A
        symbol whose fetch failed (any other ``error``) is not stored, and False
        is returned: an empty pin list would read as a part with no symbol and
        never be asked for again, so the row keeps waiting for its symbol.  False
        is also returned when the part has no ``ok`` row naming this symbol: a
        part looked up again while the symbol was in flight may name another
        symbol now, and a late answer must not overwrite it.
        """
        if symbol.status == "error" and not unreadable:
            return False
        pins = symbol.pins if symbol.status == "ok" else []
        fetched_at = int(time.time() if now is None else now)
        with closing(self.connect()) as con, con:
            cursor = con.execute(
                "UPDATE lcsc_map SET symbol_pins_json = ?, symbol_blob = ?,"
                " fetched_at = ? WHERE lcsc = ? AND status = 'ok' AND symbol_uuid = ?",
                (
                    json.dumps(
                        [{"number": pin.number, "label": pin.label} for pin in pins],
                        separators=(",", ":"),
                    ),
                    compress(symbol.shapes) if pins else None,
                    fetched_at,
                    lcsc,
                    symbol.uuid,
                ),
            )
            return cursor.rowcount > 0

    def store_footprint(
        self, footprint: FootprintRecord, now: Optional[float] = None
    ) -> None:
        """Store one footprint fetched by uuid; one without pads is not stored.

        A footprint EasyEDA has nothing for (``none``), or one it served that
        cannot be read (the client's ``Document.unreadable``), leaves nothing to
        align, so its caller stores the parts waiting on it as ``none`` rows
        (``store_lookup_miss``), which the 30-day rule then governs; otherwise
        ``needs`` would ask for the footprint again at every check.
        """
        if not footprint.pads:
            return
        fetched_at = int(time.time() if now is None else now)
        with closing(self.connect()) as con, con:
            _upsert(
                con,
                "package",
                _PACKAGE_COLUMNS,
                self._package_row(
                    footprint.puuid,
                    footprint.package_name,
                    footprint.pads,
                    footprint.footprint_shapes,
                    footprint.footprint_source,
                    footprint.footprint_origin,
                    fetched_at,
                ),
            )

    @staticmethod
    def _package_row(
        puuid: str,
        package_name: str,
        pads: list[dict],
        shapes: list[str],
        footprint_source: str,
        origin: tuple[float, float],
        fetched_at: int,
    ) -> dict[str, Any]:
        """Build a package row.

        ``origin`` is the drawing's own origin in canvas units, which ``part``
        restores as ``footprint_origin``.  The naming rule is not stored: the
        resolver reads it from the package name each time.
        """
        return {
            "puuid": puuid,
            "package_name": package_name,
            "pads_json": json.dumps(pads, separators=(",", ":")),
            "footprint_blob": compress(shapes),
            "footprint_source": footprint_source,
            "origin_x": float(origin[0]),
            "origin_y": float(origin[1]),
            "fetched_at": fetched_at,
        }

    def forget(self, lcsc: str) -> None:
        """Drop a part's row so it is looked up again when next checked; its footprint row stays."""
        with closing(self.connect()) as con, con:
            con.execute("DELETE FROM lcsc_map WHERE lcsc = ?", (lcsc,))

    def clear(self) -> None:
        """Drop every cached part and footprint."""
        with closing(self.connect()) as con, con:
            con.execute("DELETE FROM lcsc_map")
            con.execute("DELETE FROM package")
