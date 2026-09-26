"""Project-shared JLCPCB calculator metadata freshness.

Separate from frozen board stackup selection and from the orderable stackup
catalog. Rows are anonymous provider snapshots; they never include board content.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .catalog_cache import catalog_checked_at, parse_catalog_checked_at

CALCULATOR_CONFIG_CHECK_INTERVAL = timedelta(days=7)
CALCULATOR_CONFIG_SCHEMA_VERSION = 1
MAX_CALCULATOR_CONFIG_RECORDS = 256


@dataclass(frozen=True)
class CalculatorConfigCache:
    """Reusable provider models/manufacturing bounds and their last successful check."""

    models: tuple[dict[str, Any], ...] = ()
    copper: tuple[dict[str, Any], ...] = ()
    coating: tuple[dict[str, Any], ...] = ()
    limits: tuple[dict[str, Any], ...] = ()
    checked_at_utc: str = ""


def calculator_config_checked_at() -> str:
    """UTC timestamp for a complete, validated calculator-config fetch."""
    return catalog_checked_at()


def calculator_config_check_due(
    cache: CalculatorConfigCache, *, now: Optional[datetime] = None
) -> bool:
    """Refetch empty/unknown caches or those older than seven elapsed days."""
    if not (cache.models and cache.copper and cache.coating and cache.limits):
        return True
    checked = parse_catalog_checked_at(cache.checked_at_utc)
    if checked is None:
        return True
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError(
            "Calculator-config freshness requires a timezone-aware current time."
        )
    elapsed = current.astimezone(timezone.utc) - checked
    return elapsed < timedelta(0) or elapsed > CALCULATOR_CONFIG_CHECK_INTERVAL


def _bounded_records(values: object, label: str) -> tuple[dict[str, Any], ...]:
    """Require a non-empty, bounded list of JSON objects for one provider envelope."""
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_CALCULATOR_CONFIG_RECORDS
        or any(not isinstance(item, dict) for item in values)
    ):
        raise ValueError(f"Cached calculator {label} are invalid or incomplete.")
    return tuple(values)


def calculator_config_from_payload(payload: object) -> CalculatorConfigCache:
    """Decode a persisted calculator-config snapshot without migrating on read."""
    if not isinstance(payload, dict):
        raise ValueError("Cached calculator configuration must be an object.")  # noqa: TRY004
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != CALCULATOR_CONFIG_SCHEMA_VERSION
    ):
        raise ValueError("Cached calculator configuration schema is invalid.")
    required = {
        "schema_version",
        "models",
        "copper",
        "coating",
        "limits",
        "checked_at_utc",
    }
    if set(payload) != required:
        raise ValueError("Cached calculator configuration schema is invalid.")
    checked_at = payload.get("checked_at_utc", "")
    if parse_catalog_checked_at(checked_at) is None:
        checked_at = ""
    return CalculatorConfigCache(
        models=_bounded_records(payload["models"], "models"),
        copper=_bounded_records(payload["copper"], "copper parameters"),
        coating=_bounded_records(payload["coating"], "soldermask parameters"),
        limits=_bounded_records(payload["limits"], "width bounds"),
        checked_at_utc=checked_at,
    )


def calculator_config_to_payload(cache: CalculatorConfigCache) -> dict[str, Any]:
    """Encode a successful check for project.db; require a valid UTC timestamp."""
    if not isinstance(cache, CalculatorConfigCache):
        raise ValueError(  # noqa: TRY004
            "A calculator-config check must include freshness metadata."
        )
    if parse_catalog_checked_at(cache.checked_at_utc) is None:
        raise ValueError(
            "A successful calculator-config check requires a valid UTC timestamp."
        )
    _bounded_records(list(cache.models), "models")
    _bounded_records(list(cache.copper), "copper parameters")
    _bounded_records(list(cache.coating), "soldermask parameters")
    _bounded_records(list(cache.limits), "width bounds")
    return {
        "schema_version": CALCULATOR_CONFIG_SCHEMA_VERSION,
        "models": [dict(item) for item in cache.models],
        "copper": [dict(item) for item in cache.copper],
        "coating": [dict(item) for item in cache.coating],
        "limits": [dict(item) for item in cache.limits],
        "checked_at_utc": cache.checked_at_utc,
    }
