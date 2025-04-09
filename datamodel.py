"""Implementation of the Datamodel for the parts list with natural sort."""

import logging
import re

import wx.dataview as dv

from .helpers import loadIconScaled


class PartListDataModel(dv.PyDataViewModel):
    """Datamodel for use with the DataViewCtrl of the mainwindow."""

    def __init__(self, scale_factor):
        super().__init__()
        self.data = []
        self.columns = {
            "REF_COL": 0,
            "VALUE_COL": 1,
            "FP_COL": 2,
            "LCSC_COL": 3,
            "TYPE_COL": 4,
            "STOCK_COL": 5,
            "BOM_COL": 6,
            "POS_COL": 7,
            "ROT_COL": 8,
            "SIDE_COL": 9,
            "PARAMS_COL": 10,
        }

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
        self.side_icons = [
            loadIconScaled(
                "TOP.png",
                scale_factor,
            ),
            loadIconScaled(
                "BOT.png",
                scale_factor,
            ),
        ]
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def natural_sort_key(s):
        """Return a tuple that can be used for natural sorting."""
        return [
            int(text) if text.isdigit() else text.lower()
            for text in re.split("([0-9]+)", s)
        ]

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
            "string",
            "wxDataViewIconText",
            "string",
        )
        return columntypes[col]

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

    def GetValue(self, item, col):
        """Get value of an item."""
        row = self.ItemToObject(item)
        if col in [
            self.columns["BOM_COL"],
            self.columns["POS_COL"],
            self.columns["SIDE_COL"],
        ]:
            icon = row[col]
            return dv.DataViewIconText("", icon)
        return row[col]

    def SetValue(self, value, item, col):
        """Set value of an item."""
        row = self.ItemToObject(item)
        if col in [
            self.columns["BOM_COL"],
            self.columns["POS_COL"],
            self.columns["SIDE_COL"],
        ]:
            return False
        row[col] = value
        return True

    def Compare(self, item1, item2, column, ascending):
        """Override to implement natural sorting."""
        val1 = self.GetValue(item1, column)
        val2 = self.GetValue(item2, column)

        key1 = self.natural_sort_key(val1)
        key2 = self.natural_sort_key(val2)

        if ascending:
            return (key1 > key2) - (key1 < key2)
        else:
            return (key2 > key1) - (key2 < key1)

    def find_index(self, ref):
        """Get the index of a part within the data list by its reference."""
        try:
            return self.data.index([x for x in self.data if x[0] == ref].pop())
        except (ValueError, IndexError):
            return None

    def get_bom_pos_icon(self, state: str):
        """Get an icon for a state."""
        return self.bom_pos_icons[int(state)]

    def get_side_icon(self, side: str):
        """Get The side for a layer number."""
        return self.side_icons[0] if side == "0" else self.side_icons[1]

    def AddEntry(self, data: list):
        """Add a new entry to the data model."""
        data[self.columns["BOM_COL"]] = self.get_bom_pos_icon(
            data[self.columns["BOM_COL"]]
        )
        data[self.columns["POS_COL"]] = self.get_bom_pos_icon(
            data[self.columns["POS_COL"]]
        )
        data[self.columns["SIDE_COL"]] = self.get_side_icon(
            data[self.columns["SIDE_COL"]]
        )
        self.data.append(data)
        self.ItemAdded(dv.NullDataViewItem, self.ObjectToItem(data))

    def RemoveAll(self):
        """Remove all entries from the data model."""
        self.data.clear()
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
        item[self.columns["PARAMS_COL"]] = params
        self.ItemChanged(self.ObjectToItem(item))

    def remove_lcsc_number(self, item):
        """Remove the LCSC number of an item."""
        obj = self.ItemToObject(item)
        obj[self.columns["LCSC_COL"]] = ""
        obj[self.columns["TYPE_COL"]] = ""
        obj[self.columns["STOCK_COL"]] = ""
        obj[self.columns["PARAMS_COL"]] = ""
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


class PartSelectorDataModel(dv.PyDataViewModel):
    """Datamodel for use with the DataViewCtrl of the partselector modal window."""

    def __init__(self):
        super().__init__()
        self.data = []
        self.columns = {
            "LCSC_COL": 0,
            "MFR_NUMBER_COL": 1,
            "PACKAGE_COL": 2,
            "PIN_COL": 3,
            "TYPE_COL": 4,
            "PARAMS_COL": 5,
            "STOCK_COL": 6,
            "MFR_COL": 7,
            "DESCR_COL": 8,
            "PRICE_COL": 9,
        }

        self.logger = logging.getLogger(__name__)

    @staticmethod
    def natural_sort_key(s):
        """Return a tuple that can be used for natural sorting."""
        return [
            int(text) if text.isdigit() else text.lower()
            for text in re.split("([0-9]+)", s)
        ]

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
            "string",
            "string",
            "string",
        )
        return columntypes[col]

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

    def GetValue(self, item, col):
        """Get value of an item."""
        row = self.ItemToObject(item)
        return row[col]

    def SetValue(self, value, item, col):
        """Set value of an item."""
        row = self.ItemToObject(item)
        row[col] = value
        return True

    def Compare(self, item1, item2, column, ascending):
        """Override to implement natural sorting."""
        val1 = self.GetValue(item1, column)
        val2 = self.GetValue(item2, column)

        key1 = self.natural_sort_key(val1)
        key2 = self.natural_sort_key(val2)

        if ascending:
            return (key1 > key2) - (key1 < key2)
        else:
            return (key2 > key1) - (key2 < key1)

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
        return self.ItemToObject(item)[self.columns["LCSC_COL"]]

    def get_type(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["TYPE_COL"]]

    def get_stock(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["STOCK_COL"]]
