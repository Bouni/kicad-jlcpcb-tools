"""Keep supplier assembly facts per LCSC code in the shared global database.

JLC's component API is the only source of a part's assembly process and of
whether it is Standard-only, and looking a code up takes about a second. The
facts therefore persist in ``global.db`` beside corrections and part
preferences, so a board opened before shows them at once. Saved facts are
fetched again in the background once they are old, because JLC occasionally
reclassifies parts; the saved values stay in use until new ones arrive.
"""

import contextlib
import logging
import sqlite3
import time
from typing import Optional

from ..bom_estimation.assembly_mode import classify_component_product_type
from ..lcsc import normalize_lcsc

TABLE = "lcsc_assembly_metadata"
REFRESH_AFTER_S = 30 * 24 * 3600
# Writes run on the UI thread and a skipped one only costs a refetch, so a lock
# held by another program is waited on only briefly.
BUSY_TIMEOUT_S = 0.25

_CREATE = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "lcsc TEXT PRIMARY KEY, "
    "assembly_process TEXT NOT NULL DEFAULT '', "
    "component_product_type INTEGER, "
    "fetched_at REAL NOT NULL)"
)
# A result lacking one fact keeps the saved value, so two plugin windows that
# write the same code cannot erase each other's facts. Only a result carrying
# both facts renews the row's age; a partial one leaves the fact it lacked due.
_UPSERT = (
    f"INSERT INTO {TABLE} "
    "(lcsc, assembly_process, component_product_type, fetched_at) "
    "VALUES (?, ?, ?, ?) ON CONFLICT(lcsc) DO UPDATE SET "
    "assembly_process = CASE WHEN excluded.assembly_process != '' "
    f"THEN excluded.assembly_process ELSE {TABLE}.assembly_process END, "
    "component_product_type = COALESCE("
    f"excluded.component_product_type, {TABLE}.component_product_type), "
    "fetched_at = CASE WHEN excluded.assembly_process != '' "
    "AND excluded.component_product_type IS NOT NULL "
    f"THEN excluded.fetched_at ELSE {TABLE}.fetched_at END"
)


class AssemblyMetadataCache:
    """Serve assembly facts from memory and write each new fact through to disk.

    Without a path the facts last only as long as this object. A database
    problem is logged once per instance and never reaches the caller: the facts
    stay in memory, and later writes still try the database.
    """

    def __init__(
        self, path: Optional[str], logger: Optional[logging.Logger] = None
    ) -> None:
        self._path = path
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._warned = False
        # Normalised LCSC -> (assembly process, product type, time fetched).
        self._facts: dict[str, tuple[str, Optional[int], float]] = {}
        if path:
            self._load(path)

    @property
    def path(self) -> Optional[str]:
        """Return the database the facts are saved in, or None for memory only."""
        return self._path

    def get(self, lcsc: str) -> tuple[str, Optional[int]]:
        """Return the assembly process and product type, empty when unknown."""
        process, product_type, _fetched_at = self._facts.get(
            normalize_lcsc(lcsc), ("", None, 0.0)
        )
        return process, product_type

    def merge(
        self, lcsc: str, assembly_process: object, component_product_type: object
    ) -> None:
        """Record the facts a lookup returned, keeping saved ones it lacks.

        A lookup that failed returns the same empty result as a part JLC has not
        classified, so a result without either fact changes nothing. Only a
        result carrying both facts renews their age.
        """
        code = normalize_lcsc(lcsc)
        process = str(assembly_process or "").strip()
        classification = classify_component_product_type(component_product_type)
        product_type = None if classification is None else int(classification)
        if not code or (not process and product_type is None):
            return
        now = time.time()
        saved = self._facts.get(code)
        if saved is None:
            self._facts[code] = (process, product_type, now)
        else:
            saved_process, saved_type, saved_at = saved
            complete = bool(process) and product_type is not None
            self._facts[code] = (
                process or saved_process,
                saved_type if product_type is None else product_type,
                now if complete else saved_at,
            )
        self._write(code, process, product_type, now)

    def needs_fetch(self, lcsc: str, now: Optional[float] = None) -> bool:
        """Report whether a code lacks a fact or its facts are due a refresh."""
        facts = self._facts.get(normalize_lcsc(lcsc))
        if facts is None:
            return True
        process, product_type, fetched_at = facts
        if not process or product_type is None:
            return True
        current = time.time() if now is None else now
        # A fetch time in the future means the clock moved; refresh rather than
        # trust the saved facts for however long the clock was off.
        return not 0 <= current - fetched_at < REFRESH_AFTER_S

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)

    def _load(self, path: str) -> None:
        try:
            with contextlib.closing(self._connect(path)) as con, con:
                con.execute(_CREATE)
                rows = con.execute(
                    "SELECT lcsc, assembly_process, component_product_type, "
                    f"fetched_at FROM {TABLE}"
                ).fetchall()
        except (sqlite3.Error, OSError) as error:
            self._warn(error)
            return
        for lcsc, process, product_type, fetched_at in rows:
            code = normalize_lcsc(lcsc)
            if not code or not isinstance(fetched_at, (int, float)):
                continue
            classification = classify_component_product_type(product_type)
            self._facts[code] = (
                str(process or "").strip(),
                None if classification is None else int(classification),
                float(fetched_at),
            )

    def _write(
        self,
        code: str,
        process: str,
        product_type: Optional[int],
        fetched_at: float,
    ) -> None:
        path = self._path
        if not path:
            return
        try:
            with contextlib.closing(self._connect(path)) as con, con:
                # The table may be missing when loading failed earlier.
                con.execute(_CREATE)
                con.execute(_UPSERT, (code, process, product_type, fetched_at))
        except (sqlite3.Error, OSError) as error:
            self._warn(error)

    def _warn(self, error: BaseException) -> None:
        if self._warned:
            return
        self._warned = True
        self._logger.warning(
            "Supplier assembly data cannot be saved in %s and will be fetched "
            "again next time: %s",
            self._path,
            error,
        )
