"""Minimal translated copper from the reported row and two landing examples.

Only differential segments and associated flashes remain. Net names, identities,
and board coordinates are normalized; no project or manufacturing data is used.
"""

import json
from pathlib import Path
from types import ModuleType

from impedance import model


def reported_spacing_geometry(
    name: str, model_module: ModuleType = model
) -> model.BoardSnapshot:
    """Return a portable native-constructor fixture for one reported geometry."""
    cases = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("spacing_reported_geometry.json")
        .read_text()
    )
    data = cases[name]
    traces = tuple(
        model_module.Trace(
            f"{name}-{index}",
            "F.Cu",
            item["net"],
            item["width_nm"],
            tuple(tuple(point) for point in item["points"]),
        )
        for index, item in enumerate(data["traces"])
    )
    pads = []
    for index, item in enumerate(data["pads"]):
        fields = dict(item)
        fields["center"] = tuple(fields["center"])
        # This compatibility path also lets the regression fail on the older
        # geometry policy before authoritative footprint fields were introduced.
        if "footprint_id" not in model_module.CopperFeature.__dataclass_fields__:
            fields.pop("footprint_id")
            fields.pop("pad_number")
        pads.append(
            model_module.CopperFeature(
                feature_id=f"{name}-pad-{index}", layer="F.Cu", kind="pad", **fields
            )
        )
    return model_module.BoardSnapshot(
        ("F.Cu",), traces, differential_pairs=(("P", "N"),), copper_features=tuple(pads)
    )
