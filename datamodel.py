"""Implementation of the Datamodel for the parts list with natural sort."""

from collections.abc import Iterable
import logging
import re
from typing import Any

import wx  # pylint: disable=import-error
import wx.dataview as dv

from .dataview_highlight import (
    decode_highlighted_value,
    encode_highlighted_value,
    expand_footprint,
    expand_value,
)
from .helpers import loadIconScaled
from .partselector_columns import COLUMN_INDEX, MODEL_COLUMN_TYPES
from .stock_display import format_stock, stock_sort_key

STANDARD_ONLY_TOOLTIP = (
    "Part cannot be assembled in economy mode, standard must be used"
)


class _StockDataModel(dv.PyDataViewModel):
    """Share stock presentation and exact sorting between both native views."""

    def __init__(self, stock_column: int, simplify_stock: bool = True) -> None:
        super().__init__()
        self.data: list[list[Any]] = []
        self.stock_column = stock_column
        self.simplify_stock = bool(simplify_stock)

    def set_simplify_stock(self, enabled: bool) -> None:
        """Repaint stock cells when presentation changes, retaining raw values."""
        if self.simplify_stock == bool(enabled):
            return
        self.simplify_stock = bool(enabled)
        for row in self.data:
            self.ValueChanged(self.ObjectToItem(row), self.stock_column)

    def _stock_value(self, row: list[Any]) -> str:
        """Format raw supply only for display."""
        return format_stock(row[self.stock_column], self.simplify_stock)

    def _nonstock_value(self, item: Any, col: int) -> Any:
        """Return the ordinary value unless a model supplies computed cells."""
        return self.ItemToObject(item)[col]

    def GetValue(self, item: Any, col: int) -> Any:
        """Render stock consistently while allowing model-specific other cells."""
        if col == self.stock_column:
            return self._stock_value(self.ItemToObject(item))
        return self._nonstock_value(item, col)

    @staticmethod
    def natural_sort_key(value: str) -> list[Any]:
        """Return case-insensitive text and numeric chunks for natural sorting."""
        return [
            int(text) if text.isdigit() else text.lower()
            for text in re.split("([0-9]+)", value)
        ]

    def _comparison_value(self, item: Any, column: int) -> Any:
        """Return visible text for ordinary-column sorting."""
        return self.GetValue(item, column)

    def Compare(self, item1: Any, item2: Any, column: int, ascending: bool) -> int:
        """Sort exact stock independently of its label and other text naturally."""
        if column == self.stock_column:
            key1 = stock_sort_key(self.ItemToObject(item1)[column])
            key2 = stock_sort_key(self.ItemToObject(item2)[column])
        else:
            key1 = self.natural_sort_key(self._comparison_value(item1, column))
            key2 = self.natural_sort_key(self._comparison_value(item2, column))
        order = (key1 > key2) - (key1 < key2)
        return order if ascending else -order


class PartListDataModel(_StockDataModel):
    """Datamodel for use with the DataViewCtrl of the mainwindow."""

    # The TRAILING_SPACER_COL is used to ensure that the last visible column
    # (PRICE_COL) doesn't stretch when the control is wider than the total
    # column width. It contains an empty string and is hidden from view, but
    # it allows the PRICE_COL to maintain a consistent width.
    columns = {
        "REF_COL": 0,
        "VALUE_COL": 1,
        "FP_COL": 2,
        "LCSC_COL": 3,
        "TYPE_COL": 4,
        "STOCK_COL": 5,
        "BOM_COL": 6,
        "POS_COL": 7,
        "DNP_COL": 8,
        "ROT_COL": 9,
        "SIDE_COL": 10,
        "PARAMS_COL": 11,
        "ENRICH_COL": 12,
        "PRICE_COL": 13,
        "TRAILING_SPACER_COL": 14,
        "STANDARD_ONLY_COL": 15,
    }

    def __init__(self, scale_factor: float, simplify_stock: bool = True) -> None:
        super().__init__(self.columns["STOCK_COL"], simplify_stock)
        self.standard_only_refs = set()
        self.stock_concern_refs: set[str] = set()

        self.bom_pos_icons = [
            loadIconScaled(
                "mdi-check-color.png",
                scale_factor,
            ),
            loadIconScaled(
                "mdi-close-color.png",
                scale_factor,
            ),
        ]
        self.logger = logging.getLogger(__name__)

    def set_standard_only_refs(self, refs):
        """Set references whose JLC classification is Standard Only."""
        updated_refs = set(refs or ())
        changed_refs = self.standard_only_refs.symmetric_difference(updated_refs)
        self.standard_only_refs = updated_refs
        column = self.columns["STANDARD_ONLY_COL"]
        for row in self.data:
            if str(row[self.columns["REF_COL"]] or "") in changed_refs:
                self.ValueChanged(self.ObjectToItem(row), column)

    def is_standard_only(self, item):
        """Return whether an item's reference is classified Standard Only."""
        row = self.ItemToObject(item)
        return bool(
            row and str(row[self.columns["REF_COL"]] or "") in self.standard_only_refs
        )

    def set_stock_concern_refs(self, refs: Iterable[str]) -> None:
        """Notify Stock cells whose concern state changed, including cleared marks."""
        updated_refs = set(refs)
        changed_refs = self.stock_concern_refs.symmetric_difference(updated_refs)
        self.stock_concern_refs = updated_refs
        for row in self.data:
            if str(row[self.columns["REF_COL"]] or "") in changed_refs:
                self.ValueChanged(self.ObjectToItem(row), self.columns["STOCK_COL"])

    def GetAttr(self, item: Any, col: int, attr: Any) -> bool:
        """Style concerned Stock cells and the existing TOP/BOT Side labels."""
        if col == self.columns["STOCK_COL"]:
            row = self.ItemToObject(item)
            if (
                row
                and str(row[self.columns["REF_COL"]] or "") in self.stock_concern_refs
            ):
                background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
                colour = (
                    wx.Colour(255, 211, 102)
                    if background.GetLuminance() < 0.5
                    else wx.Colour(128, 52, 0)
                )
                # Cocoa retains custom cell backgrounds after their attributes
                # clear. Foreground-only concern preserves native row striping.
                attr.SetColour(colour)
                attr.SetBold(True)
                return True
            return False
        if col == self.columns["SIDE_COL"]:
            row = self.ItemToObject(item)
            if not row:
                return False
            side_colours = {
                "TOP": wx.Colour(200, 52, 52),
                "BOT": wx.Colour(77, 127, 196),
            }
            colour = side_colours.get(row[col])
            if colour is None:
                return False
            attr.SetColour(colour)
            if hasattr(attr, "SetBold"):
                attr.SetBold(True)
            return True
        return False

    def GetColumnCount(self):
        """Get number of columns."""
        return len(self.columns)

    def GetColumnType(self, col):
        """Get type of each column."""
        columntypes = (
            "string",
            "string",
            "string",
            "string",
            "string",
            "string",
            "wxDataViewIconText",
            "wxDataViewIconText",
            "wxDataViewIconText",
            "string",
            "string",
            "string",
            "string",
            "string",
            "string",
            "bool",
        )
        return columntypes[col]

    def HasValue(self, item, col):
        """Leave non-Standard cells blank in the computed toggle column."""
        if col == self.columns["STANDARD_ONLY_COL"]:
            return self.is_standard_only(item)
        return super().HasValue(item, col)

    def GetChildren(self, parent, children):
        """Get child items of a parent."""
        if not parent:
            for row in self.data:
                children.append(self.ObjectToItem(row))
            return len(self.data)
        return 0

    def IsContainer(self, item):
        """Check if tem is a container."""
        return not item

    def GetParent(self, item):
        """Get parent item."""
        return dv.NullDataViewItem

    def _stock_value(self, row: list[Any]) -> str:
        """Give assigned unknown availability a visible concern target."""
        value = super()._stock_value(row)
        if not value.strip():
            return "?" if str(row[self.columns["LCSC_COL"]] or "").strip() else ""
        return value

    def _nonstock_value(self, item: Any, col: int) -> Any:
        """Keep main-table computed indicators and native icon values."""
        row = self.ItemToObject(item)
        if col == self.columns["STANDARD_ONLY_COL"]:
            return self.HasValue(item, col)
        if col in [
            self.columns["BOM_COL"],
            self.columns["POS_COL"],
            self.columns["DNP_COL"],
        ]:
            icon = row[col]
            return dv.DataViewIconText("", icon)
        return row[col]

    def _encode_params_value(
        self,
        reference: str,
        value: str,
        footprint: str,
        params: str,
    ) -> str:
        """Store params display text together with row-specific highlight terms."""
        value_terms = expand_value(reference, value)
        footprint_terms = expand_footprint(reference, footprint)
        return encode_highlighted_value(
            params,
            [*value_terms, *footprint_terms],
        )

    @staticmethod
    def _decode_params_value(value: str) -> str:
        """Return only the visible params text from an encoded params cell value."""
        return decode_highlighted_value(value)[0]

    def SetValue(self, value, item, col):
        """Set value of an item."""
        row = self.ItemToObject(item)
        if col in [
            self.columns["BOM_COL"],
            self.columns["POS_COL"],
            self.columns["DNP_COL"],
            self.columns["SIDE_COL"],
            self.columns["STANDARD_ONLY_COL"],
        ]:
            return False
        row[col] = value
        return True

    def _comparison_value(self, item: Any, column: int) -> Any:
        """Ignore hidden Params highlight metadata when comparing visible text."""
        value = super()._comparison_value(item, column)
        if column == self.columns["PARAMS_COL"]:
            return self._decode_params_value(value)
        return value

    def find_index(self, ref):
        """Get the index of a part within the data list by its reference."""
        try:
            return self.data.index([x for x in self.data if x[0] == ref].pop())
        except (ValueError, IndexError):
            return None

    def get_bom_pos_icon(self, state: str):
        """Get an icon for a state."""
        return self.bom_pos_icons[int(state)]

    @staticmethod
    def get_side_label(side: str) -> str:
        """Get the display label for a layer number."""
        return "TOP" if side == "0" else "BOT"

    def AddEntry(self, data: list):
        """Add a new entry to the data model."""
        if len(data) <= self.columns["PRICE_COL"]:
            data.append("")
        if len(data) <= self.columns["TRAILING_SPACER_COL"]:
            data.append("")
        else:
            data[self.columns["TRAILING_SPACER_COL"]] = ""

        data[self.columns["BOM_COL"]] = self.get_bom_pos_icon(
            data[self.columns["BOM_COL"]]
        )
        data[self.columns["POS_COL"]] = self.get_bom_pos_icon(
            data[self.columns["POS_COL"]]
        )
        data[self.columns["DNP_COL"]] = self.get_bom_pos_icon(
            data[self.columns["DNP_COL"]]
        )
        data[self.columns["SIDE_COL"]] = self.get_side_label(
            data[self.columns["SIDE_COL"]]
        )
        data[self.columns["PARAMS_COL"]] = self._encode_params_value(
            reference=str(data[self.columns["REF_COL"]] or ""),
            value=str(data[self.columns["VALUE_COL"]] or ""),
            footprint=str(data[self.columns["FP_COL"]] or ""),
            params=str(data[self.columns["PARAMS_COL"]] or ""),
        )
        self.data.append(data)
        self.ItemAdded(dv.NullDataViewItem, self.ObjectToItem(data))

    def RemoveAll(self) -> None:
        """Remove all entries from the data model."""
        self.data.clear()
        self.standard_only_refs.clear()
        self.stock_concern_refs.clear()
        self.Cleared()

    def get_all(self):
        """Get tall items."""
        return self.data

    def get_reference(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["REF_COL"]]

    def get_value(self, item):
        """Get the value of an item."""
        return self.ItemToObject(item)[self.columns["VALUE_COL"]]

    def get_lcsc(self, item):
        """Get the lcsc of an item."""
        return self.ItemToObject(item)[self.columns["LCSC_COL"]]

    def get_footprint(self, item):
        """Get the footprint of an item."""
        return self.ItemToObject(item)[self.columns["FP_COL"]]

    def select_alike(self, item):
        """Select all items that have the same value and footprint."""
        obj = self.ItemToObject(item)
        alike = []
        for data in self.data:
            if data[1:3] == obj[1:3]:
                alike.append(self.ObjectToItem(data))
        return alike

    def set_lcsc(self, ref, lcsc, type, stock, params):
        """Set an lcsc number, type and stock for given reference."""
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["LCSC_COL"]] = lcsc
        item[self.columns["TYPE_COL"]] = type
        item[self.columns["STOCK_COL"]] = stock
        item[self.columns["PARAMS_COL"]] = self._encode_params_value(
            reference=str(item[self.columns["REF_COL"]] or ""),
            value=str(item[self.columns["VALUE_COL"]] or ""),
            footprint=str(item[self.columns["FP_COL"]] or ""),
            params=str(params or ""),
        )
        item[self.columns["ENRICH_COL"]] = ""
        item[self.columns["PRICE_COL"]] = ""
        self.standard_only_refs.discard(ref)
        self.ItemChanged(self.ObjectToItem(item))

    def set_catalog_details(
        self, lcsc: str, part_type: str, stock: object, params: str
    ) -> None:
        """Refresh catalog-owned fields without resetting board or estimator state."""
        target = lcsc.strip().upper()
        if not target:
            return
        for row in self.data:
            if str(row[self.columns["LCSC_COL"]] or "").strip().upper() != target:
                continue
            row[self.columns["TYPE_COL"]] = part_type
            row[self.columns["STOCK_COL"]] = stock
            row[self.columns["PARAMS_COL"]] = self._encode_params_value(
                reference=str(row[self.columns["REF_COL"]] or ""),
                value=str(row[self.columns["VALUE_COL"]] or ""),
                footprint=str(row[self.columns["FP_COL"]] or ""),
                params=params,
            )
            self.ItemChanged(self.ObjectToItem(row))

    def set_correction(self, ref, correction):
        """Show the correction rule now selected for the given reference."""
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["ROT_COL"]] = correction
        self.ItemChanged(self.ObjectToItem(item))

    def set_bom_price(self, ref, price_label):
        """Set BOM price text for a given part reference."""
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["PRICE_COL"]] = price_label
        self.ItemChanged(self.ObjectToItem(item))

    def set_enrichment_status(self, ref, status):
        """Set enrichment status text for a given part reference."""
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["ENRICH_COL"]] = status
        self.ItemChanged(self.ObjectToItem(item))

    def remove_lcsc_number(self, item):
        """Remove the LCSC number of an item."""
        obj = self.ItemToObject(item)
        self.standard_only_refs.discard(str(obj[self.columns["REF_COL"]] or ""))
        obj[self.columns["LCSC_COL"]] = ""
        obj[self.columns["TYPE_COL"]] = ""
        obj[self.columns["STOCK_COL"]] = ""
        obj[self.columns["PARAMS_COL"]] = ""
        obj[self.columns["ENRICH_COL"]] = ""
        obj[self.columns["PRICE_COL"]] = ""
        self.ItemChanged(self.ObjectToItem(obj))

    def toggle_bom(self, item):
        """Toggle BOM for a given item."""
        obj = self.ItemToObject(item)
        if obj[self.columns["BOM_COL"]] == self.bom_pos_icons[0]:
            obj[self.columns["BOM_COL"]] = self.bom_pos_icons[1]
        else:
            obj[self.columns["BOM_COL"]] = self.bom_pos_icons[0]
        self.ItemChanged(self.ObjectToItem(obj))

    def toggle_pos(self, item):
        """Toggle POS for a given item."""
        obj = self.ItemToObject(item)
        if obj[self.columns["POS_COL"]] == self.bom_pos_icons[0]:
            obj[self.columns["POS_COL"]] = self.bom_pos_icons[1]
        else:
            obj[self.columns["POS_COL"]] = self.bom_pos_icons[0]
        self.ItemChanged(self.ObjectToItem(obj))

    def toggle_bom_pos(self, item):
        """Toggle BOM and POS for a given item."""
        self.toggle_bom(item)
        self.toggle_pos(item)


class PartSelectorDataModel(_StockDataModel):
    """Datamodel for use with the DataViewCtrl of the partselector modal window."""

    def __init__(self, simplify_stock: bool = True) -> None:
        self.columns = dict(COLUMN_INDEX)
        super().__init__(self.columns["stock"], simplify_stock)

        self.logger = logging.getLogger(__name__)

    def GetColumnCount(self):
        """Get number of columns."""
        return len(self.columns)

    def GetColumnType(self, col):
        """Get type of each column."""
        return MODEL_COLUMN_TYPES[col]

    def GetChildren(self, parent, children):
        """Get child items of a parent."""
        if not parent:
            for row in self.data:
                children.append(self.ObjectToItem(row))
            return len(self.data)
        return 0

    def IsContainer(self, item):
        """Check if tem is a container."""
        return not item

    def GetParent(self, item):
        """Get parent item."""
        return dv.NullDataViewItem

    def SetValue(self, value, item, col):
        """Set value of an item."""
        row = self.ItemToObject(item)
        row[col] = value
        return True

    def find_index(self, ref):
        """Get the index of a part within the data list by its reference."""
        try:
            return self.data.index([x for x in self.data if x[0] == ref].pop())
        except (ValueError, IndexError):
            return None

    def AddEntry(self, data: list):
        """Add a new entry to the data model."""
        self.data.append(data)
        self.ItemAdded(dv.NullDataViewItem, self.ObjectToItem(data))

    def RemoveAll(self):
        """Remove all entries from the data model."""
        self.data.clear()
        self.Cleared()

    def get_all(self):
        """Get tall items."""
        return self.data

    def get_lcsc(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["lcsc"]]

    def get_type(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["type"]]

    def get_stock(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["stock"]]
