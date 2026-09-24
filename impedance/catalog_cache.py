"""Catalog-level freshness, separate from frozen board and stackup provenance."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Optional

from .stackup_model import Stackup

CATALOG_CHECK_INTERVAL = timedelta(days=1)
_CHECKED_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)


@dataclass(frozen=True)
class CatalogCache:
    """A project-shared catalog and its latest successful complete check.

    Empty/unknown check times are legitimate for old caches. A board's selected
    stackup is not part of this cache and cannot establish catalog freshness.
    """

    stackups: tuple[Stackup, ...] = ()
    checked_at_utc: str = ""


def parse_catalog_checked_at(value: object) -> Optional[datetime]:
    """Treat absent or malformed freshness metadata as unknown, retaining rows."""
    if not isinstance(value, str) or _CHECKED_AT_PATTERN.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def catalog_checked_at() -> str:
    """Read UTC when a complete, validated catalog check succeeds, not on open."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def catalog_check_due(cache: CatalogCache, *, now: Optional[datetime] = None) -> bool:
    """Check empty/unknown caches or those more than 24 elapsed hours old.

    Call with the compatible, unfiltered catalog, excluding a frozen board
    selection. A successful empty response still requires a check next opening.
    Future timestamps are not trusted to suppress checks after a clock change.
    """
    if not cache.stackups:
        return True
    checked = parse_catalog_checked_at(cache.checked_at_utc)
    if checked is None:
        return True
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError("Catalog freshness requires a timezone-aware current time.")
    elapsed = current.astimezone(timezone.utc) - checked
    return elapsed < timedelta(0) or elapsed > CATALOG_CHECK_INTERVAL
