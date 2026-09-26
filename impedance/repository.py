"""Persist one board's controlled-impedance intent through project.db."""

from typing import Any, Optional, Protocol

from .calculator_config_cache import (
    CalculatorConfigCache,
    calculator_config_from_payload,
    calculator_config_to_payload,
)
from .catalog_cache import CatalogCache, parse_catalog_checked_at
from .model import Config, ValidationError, validate_config
from .stackup_model import Stackup, stackup_from_dict, stackup_to_dict, validate_stackup

# ImpedanceDatabase versions its storage envelope independently of the payload.
# Payload upgrades remain read-only until an explicit save; they do not migrate
# the database schema or change ImpedanceDatabase.CONFIG_VERSION.
DATABASE_RECORD_VERSION = 1
CATALOG_CACHE_VERSION = 2
MAX_CATALOG_STACKUPS = 2_000


class ConfigDatabase(Protocol):
    """Expose board-scoped intent and project-shared public catalog operations."""

    def ensure_current_board(self, board_id: str, board_path: str) -> None:
        """Reject a stale window before reading or writing another PCB's intent."""
        ...

    def load_config(self, board_id: str) -> Optional[dict[str, Any]]:
        """Return the saved payload and its database revision, if present."""
        ...

    def save_config(
        self,
        board_id: str,
        payload: dict[str, Any],
        enabled: bool,
        expected_revision: int,
    ) -> int:
        """Atomically save if the expected revision is still current."""
        ...

    def config_reset_token(self, board_id: str) -> str:
        """Fingerprint the exact stored row for an explicit, race-safe reset."""
        ...

    def reset_config(
        self, board_id: str, expected_token: str, empty_payload: dict[str, Any]
    ) -> int:
        """Replace the confirmed, unchanged record with disabled defaults."""
        ...

    def load_stackup_catalog(self, layer_count: int) -> Optional[dict[str, Any]]:
        """Return the project-shared public catalog cache, if present."""
        ...

    def save_stackup_catalog(self, layer_count: int, payload: dict[str, Any]) -> None:
        """Save reusable catalog data independently from board edit revisions."""
        ...

    def load_calculator_config(self) -> Optional[dict[str, Any]]:
        """Return the project-shared calculator metadata cache, if present."""
        ...

    def save_calculator_config(self, payload: dict[str, Any]) -> None:
        """Save calculator metadata independently from board edit revisions."""
        ...


class ImpedanceRepository:
    """Keep persistence validation and concurrent-editor checks outside the UI."""

    def __init__(self, database: ConfigDatabase, board_id: str) -> None:
        if not isinstance(board_id, str) or not board_id.strip():
            raise ValidationError("A saved board identity is required.")
        self.database = database
        self.board_id = board_id

    def load(self) -> tuple[Config, int]:
        """Load validated intent; malformed data stays intact and raises an error."""
        try:
            record = self.database.load_config(self.board_id)
            if record is None:
                return Config(), 0
            if not isinstance(record, dict) or set(record) != {
                "version",
                "revision",
                "enabled",
                "payload",
            }:
                raise ValidationError("Invalid impedance database record.")
            if (
                type(record["version"]) is not int
                or record["version"] != DATABASE_RECORD_VERSION
            ):
                raise ValidationError("Unsupported impedance database version.")
            revision = record["revision"]
            if type(revision) is not int or revision <= 0:
                raise ValidationError("Invalid impedance database revision.")
            if type(record["enabled"]) is not bool:
                raise ValidationError("Invalid impedance database enable state.")
            config = Config.from_dict(record["payload"])
            if config.enabled != record["enabled"]:
                raise ValidationError("The impedance enable state is inconsistent.")
        except (ValueError, TypeError) as error:
            raise ValidationError(
                f"Cannot load this board's controlled-impedance configuration: {error}"
            ) from error
        return config, revision

    def save(self, config: Config, expected_revision: int) -> int:
        """Validate before writing and preserve optimistic revision conflicts."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValidationError(
                "The expected configuration revision must be nonnegative."
            )
        validate_config(config)
        payload = config.to_dict()
        return self.database.save_config(
            self.board_id, payload, config.enabled, expected_revision
        )

    @staticmethod
    def _catalog_values(layer_count: int, stackups: tuple[Stackup, ...]) -> None:
        """Bound a complete same-layer-count catalog and require unique source IDs."""
        if type(layer_count) is not int or not 2 <= layer_count <= 64:
            raise ValidationError(
                "Catalog copper-layer count must be between 2 and 64."
            )
        if not isinstance(stackups, tuple) or len(stackups) > MAX_CATALOG_STACKUPS:
            raise ValidationError(
                "Cached stackup catalog must be a bounded immutable collection."
            )
        seen: set[str] = set()
        for stackup in stackups:
            validate_stackup(stackup)
            if stackup.layer_count != layer_count:
                raise ValidationError(
                    "Cached stackup has a different copper-layer count."
                )
            if stackup.stackup_id in seen:
                raise ValidationError("Cached stackup catalog repeats a provider ID.")
            seen.add(stackup.stackup_id)

    def load_stackup_catalog(self, layer_count: int) -> CatalogCache:
        """Load rows and freshness without writing a migration or board settings."""
        return self.read_stackup_catalog(self.database, layer_count)

    @classmethod
    def read_stackup_catalog(
        cls, database: ConfigDatabase, layer_count: int
    ) -> CatalogCache:
        """Read the project-shared catalog without requiring a registered PCB."""
        cls._catalog_values(layer_count, ())
        payload = database.load_stackup_catalog(layer_count)
        if payload is None:
            return CatalogCache()
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] not in (1, CATALOG_CACHE_VERSION)
        ):
            raise ValidationError(
                "Cached stackup catalog schema is invalid; a new catalog check is needed."
            )
        fields = {"schema_version", "stackups"}
        accepted_fields = (
            (fields,)
            if payload["schema_version"] == 1
            else (
                fields,
                fields | {"checked_at_utc"},
            )
        )
        if (
            set(payload) not in accepted_fields
            or not isinstance(payload.get("stackups"), list)
            or len(payload["stackups"]) > MAX_CATALOG_STACKUPS
        ):
            raise ValidationError(
                "Cached stackup catalog schema is invalid; a new catalog check is needed."
            )
        result = tuple(stackup_from_dict(item) for item in payload["stackups"])
        cls._catalog_values(layer_count, result)
        checked_at = payload.get("checked_at_utc", "")
        # Version 1 never recorded complete-check time. Per-stackup retrieval
        # dates cannot substitute for that evidence. Malformed time metadata
        # likewise makes valid cached choices due without discarding them.
        if parse_catalog_checked_at(checked_at) is None:
            checked_at = ""
        return CatalogCache(result, checked_at)

    def save_stackup_catalog(self, layer_count: int, cache: CatalogCache) -> None:
        """Atomically cache a successful check and rows, never a board selection."""
        if not isinstance(cache, CatalogCache):
            raise ValidationError(
                "A catalog check must include rows and freshness metadata."
            )
        self._catalog_values(layer_count, cache.stackups)
        if parse_catalog_checked_at(cache.checked_at_utc) is None:
            raise ValidationError(
                "A successful catalog check requires a valid UTC timestamp."
            )
        self.database.save_stackup_catalog(
            layer_count,
            {
                "schema_version": CATALOG_CACHE_VERSION,
                "stackups": [stackup_to_dict(stackup) for stackup in cache.stackups],
                "checked_at_utc": cache.checked_at_utc,
            },
        )

    def load_calculator_config(self) -> CalculatorConfigCache:
        """Load provider calculator metadata without writing a migration."""
        return self.read_calculator_config(self.database)

    @classmethod
    def read_calculator_config(cls, database: ConfigDatabase) -> CalculatorConfigCache:
        """Read the project-shared calculator cache without requiring a PCB."""
        payload = database.load_calculator_config()
        if payload is None:
            return CalculatorConfigCache()
        try:
            return calculator_config_from_payload(payload)
        except ValueError as error:
            raise ValidationError(
                f"Cached calculator configuration is invalid; a new fetch is needed: {error}"
            ) from error

    def save_calculator_config(self, cache: CalculatorConfigCache) -> None:
        """Atomically cache a successful calculator-config check."""
        try:
            payload = calculator_config_to_payload(cache)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        self.database.save_calculator_config(payload)
