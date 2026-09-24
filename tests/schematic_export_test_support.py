"""Capture real export snapshots from small stateful board fixtures."""

from collections.abc import Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any


def snapshot_from_parts(
    capture: Callable[[Any], Any], parts: Iterable[Mapping[str, Any]]
) -> Any:
    """Turn source rows into native fields before invoking production capture."""
    footprints = []
    for part in parts:
        fields = dict(part["fields"] if "fields" in part else {"LCSC": part["lcsc"]})
        footprints.append(
            SimpleNamespace(
                GetReference=lambda row=part: row["reference"],
                GetAttributes=lambda row=part: 8 if row["exclude_from_bom"] else 0,
                GetFields=lambda values=fields: [
                    SimpleNamespace(
                        GetName=lambda key=name: key,
                        GetText=lambda content=value: content,
                    )
                    for name, value in values.items()
                ],
            )
        )
    return capture(SimpleNamespace(GetFootprints=lambda: footprints))
