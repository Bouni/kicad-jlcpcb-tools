"""Real native data types and stateful doubles for KiCad's public variant API."""

# Stateful native API doubles retain the C++ method spellings.
# ruff: noqa: D101, D102, D103
from copy import deepcopy
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Optional

PACKAGE = "_variant_test"
package = sys.modules.setdefault(PACKAGE, ModuleType(PACKAGE))
package.__path__ = [str(Path(__file__).resolve().parents[1])]
native = importlib.import_module(f"{PACKAGE}.variant.native")

# Verified against KiCad 10's native constants, independently of the adapter.
KICAD_FP_DNP = 64
KICAD_FP_JUST_ADDED = 32


class Variant:
    def __init__(self, name: str = "") -> None:
        self.name = name
        self.fields: dict[str, str] = {}
        self.dnp = self.bom = self.pos = False

    def GetFields(self) -> dict[str, str]:
        return dict(self.fields)

    def HasFieldValue(self, name: str) -> bool:
        return name in self.fields

    def GetFieldValue(self, name: str) -> str:
        return self.fields.get(name, "")

    def SetFieldValue(self, name: str, value: str) -> None:
        self.fields[name] = value

    def GetDNP(self) -> bool:
        return self.dnp

    def SetDNP(self, value: bool) -> None:
        self.dnp = value

    def GetExcludedFromBOM(self) -> bool:
        return self.bom

    def SetExcludedFromBOM(self, value: bool) -> None:
        self.bom = value

    def GetExcludedFromPosFiles(self) -> bool:
        return self.pos

    def SetExcludedFromPosFiles(self, value: bool) -> None:
        self.pos = value


class Field:
    def __init__(self, fp: Any, name: str) -> None:
        self.fp = fp
        self.name = name

    def GetName(self) -> str:
        return self.name

    def GetText(self) -> str:
        return self.fp.fields[self.name]

    def SetVisible(self, visible: bool) -> None:
        self.fp.visible[self.name] = visible


class Footprint:
    def __init__(self, board: Any, component: str, ref: str = "R1") -> None:
        self.board = board
        self.m_Uuid = SimpleNamespace(AsString=lambda: component)
        self.fields = {"Reference": ref, "Value": "10k", "LCSC": "C1"}
        self.visible: dict[str, bool] = {}
        self.variants: dict[str, Variant] = {}
        self.attributes = 0
        self.fail_field = ""

    def GetFields(self) -> list[Field]:
        return [Field(self, name) for name in self.fields]

    def Remove(self, field: Field) -> None:
        del self.fields[field.name]

    def GetField(self, name: str) -> Any:
        return Field(self, name) if name in self.fields else None

    def SetField(self, name: str, value: str) -> None:
        self.fields[name] = value
        if self.fail_field == name:
            self.fail_field = ""
            raise RuntimeError("simulated partial native failure")

    def GetReference(self) -> str:
        return self.fields["Reference"]

    def GetValue(self) -> str:
        return self.fields["Value"]

    def Value(self) -> Field:
        return Field(self, "Value")

    def GetFPIDAsString(self) -> str:
        return "R:R0603"

    def GetPosition(self) -> Any:
        return SimpleNamespace(x=10_000_000, y=20_000_000)

    def GetOrientationDegrees(self) -> float:
        return 90.0

    def IsFlipped(self) -> bool:
        return False

    def GetLayer(self) -> int:
        return 31 if self.IsFlipped() else 0

    def Pads(self) -> list[Any]:
        return [
            SimpleNamespace(GetAttribute=lambda: 1, HasHole=lambda: False)
            for _ in range(2)
        ]

    def GetAttributes(self) -> int:
        return self.attributes

    def SetAttributes(self, attributes: int) -> None:
        self.attributes = attributes

    def IsDNP(self) -> bool:
        return bool(self.attributes & KICAD_FP_DNP)

    def IsExcludedFromBOM(self) -> bool:
        return bool(self.attributes & 8)

    def IsExcludedFromPosFiles(self) -> bool:
        return bool(self.attributes & 4)

    def GetVariant(self, name: str) -> Any:
        return self.variants.get(name)

    def AddVariant(self, name: str) -> Variant:
        if name not in self.variants:
            v = Variant(name)
            v.dnp, v.bom, v.pos = (
                self.IsDNP(),
                self.IsExcludedFromBOM(),
                self.IsExcludedFromPosFiles(),
            )
            self.variants[name] = v
        return self.variants[name]

    def SetVariant(self, variant: Variant) -> None:
        self.variants[variant.name] = deepcopy(variant)

    def DeleteVariant(self, name: str) -> None:
        self.variants.pop(name, None)

    def GetFieldValueForVariant(self, name: str, field: str) -> str:
        variant = self.GetVariant(name)
        if variant and variant.HasFieldValue(field):
            return variant.GetFieldValue(field)
        return self.fields.get(field, "")

    def GetDNPForVariant(self, name: str) -> bool:
        v = self.GetVariant(name)
        return v.dnp if v else self.IsDNP()

    def GetExcludedFromBOMForVariant(self, name: str) -> bool:
        v = self.GetVariant(name)
        return v.bom if v else self.IsExcludedFromBOM()

    def GetExcludedFromPosFilesForVariant(self, name: str) -> bool:
        v = self.GetVariant(name)
        return v.pos if v else self.IsExcludedFromPosFiles()

    def SetModified(self) -> None:
        self.board.modified = True


class Board:
    def __init__(self) -> None:
        self.m_Uuid = SimpleNamespace(AsString=lambda: "board-uuid")
        self.names = ["A", "B"]
        self.current = "A"
        self.modified = False
        self.parts = [Footprint(self, "component-1")]

    def GetFileName(self) -> str:
        return "/project/board.kicad_pcb"

    def GetVariantNamesForUI(self) -> list[str]:
        return ["< Par défaut >", *self.names]

    def GetVariantDescription(self, name: str) -> str:
        return f"Description {name}"

    def GetCurrentVariant(self) -> str:
        return self.current

    def HasVariant(self, name: str) -> bool:
        return any(n.casefold() == name.casefold() for n in self.names)

    def GetFootprints(self) -> list[Footprint]:
        return list(self.parts)

    def FindFootprintByReference(self, reference: str) -> Optional[Footprint]:
        return next(
            (part for part in self.parts if part.GetReference() == reference), None
        )

    def GetTimeStamp(self) -> int:
        return 0


def assignment(
    status: str = "valid", inherited: bool = False
) -> native.ResolvedAssignment:
    """Keep assignment validity and native provenance independently configurable."""
    return native.ResolvedAssignment(status, inherited, ())


def State(
    component_id: str, reference: str, variant_name: str, **changes: Any
) -> native.ComponentVariantState:
    """Build real immutable component state with ordinary resistor defaults."""
    defaults = {
        "value": "10k",
        "lcsc": "C123",
        "assignment": assignment(),
        "bom": True,
        "pos": True,
        "pop": True,
        "footprint": "Resistor_SMD:R_0603",
        "side": "top",
        "pcb_angle": 0.0,
        "x_mm": 0.0,
        "y_mm": 0.0,
        "pad_count": 2,
        "has_tht": False,
        "attributes": 0,
        "source_revision": "r1",
        "footprint_field": "Resistor_SMD:R_0603",
    }
    defaults.update(changes)
    if "assignment" not in changes:
        defaults["assignment"] = assignment("valid" if defaults["lcsc"] else "empty")
    return native.ComponentVariantState(
        component_id, reference, variant_name, **defaults
    )


def Snapshot(
    states: tuple[native.ComponentVariantState, ...],
    names: Optional[tuple[str, ...]] = None,
) -> native.BoardVariantSnapshot:
    """Use production identity lookups and captured native targets in every test."""
    if names is None:
        names = tuple(dict.fromkeys(state.variant_name for state in states)) or (
            "",
            "A",
            "B",
        )
    return native.BoardVariantSnapshot(
        "board-one",
        "live-board-one",
        "source-one",
        tuple(native.VariantDefinition(name, name or "Default") for name in names),
        states,
        tuple(dict.fromkeys(state.component_id for state in states)),
        "",
    )
