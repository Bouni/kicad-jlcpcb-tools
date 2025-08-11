"""Contains the part details modal dialog."""

import logging
from pathlib import Path
import webbrowser

import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

from .events import MessageEvent
from .helpers import HighResWxSize, loadBitmapScaled
from .lcsc_api import LCSC_API


class PartDetailsDialog(wx.Dialog):
    """The part details dialog class."""

    def __init__(self, parent, part):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB Part Details",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(1000, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.STAY_ON_TOP,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.part = part
        self.datasheet_path = Path(self.parent.project_path) / "datasheets"
        self.lcsc_api = LCSC_API()
        self.pdfurl = ""
        self.pageurl = ""
        self.picture = None

        # ---------------------------------------------------------------------
        # ---------------------------- Hotkeys --------------------------------
        # ---------------------------------------------------------------------
        quitid = wx.NewId()
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)

        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), quitid)
        entries[1].Set(wx.ACCEL_CTRL, ord("Q"), quitid)
        entries[2].Set(wx.ACCEL_SHIFT, wx.WXK_ESCAPE, quitid)
        accel = wx.AcceleratorTable(entries)
        self.SetAcceleratorTable(accel)

        # ---------------------------------------------------------------------
        # ----------------------- Properties List -----------------------------
        # ---------------------------------------------------------------------
        self.data_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_SINGLE,
        )
        self.property = self.data_list.AppendTextColumn(
            "Property",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(self.parent.scale_factor * 200),
            align=wx.ALIGN_LEFT,
        )
        self.value = self.data_list.AppendTextColumn(
            "Value",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(self.parent.scale_factor * 300),
            align=wx.ALIGN_LEFT,
        )

        # ---------------------------------------------------------------------
        # ------------------------- Right side ------------------------------
        # ---------------------------------------------------------------------
        self.image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("placeholder.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 200)),
            0,
        )
        self.savepdf_button = wx.Button(
            self,
            wx.ID_ANY,
            "Download Datasheet",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.openpdf_button = wx.Button(
            self,
            wx.ID_ANY,
            "Open Datasheet",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.openpage_button = wx.Button(
            self,
            wx.ID_ANY,
            "Open LCSC page",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.savepdf_button.Bind(wx.EVT_BUTTON, self.savepdf)
        self.openpdf_button.Bind(wx.EVT_BUTTON, self.openpdf)
        self.openpage_button.Bind(wx.EVT_BUTTON, self.openpage)

        self.savepdf_button.SetBitmap(
            loadBitmapScaled(
                "mdi-cloud-download-outline.png",
                self.parent.scale_factor,
            )
        )
        self.savepdf_button.SetBitmapMargins((2, 0))

        self.openpdf_button.SetBitmap(
            loadBitmapScaled(
                "mdi-file-document-outline.png",
                self.parent.scale_factor,
            )
        )
        self.openpdf_button.SetBitmapMargins((2, 0))

        self.openpage_button.SetBitmap(
            loadBitmapScaled(
                "mdi-earth.png",
                self.parent.scale_factor,
            )
        )
        self.openpage_button.SetBitmapMargins((2, 0))

        # ---------------------------------------------------------------------
        # ------------------------ Layout and Sizers --------------------------
        # ---------------------------------------------------------------------

        right_side_layout = wx.BoxSizer(wx.VERTICAL)
        right_side_layout.Add(self.image, 10, wx.ALL | wx.EXPAND, 5)
        right_side_layout.AddStretchSpacer(50)
        right_side_layout.Add(self.savepdf_button, 5, wx.LEFT | wx.RIGHT | wx.EXPAND, 5)
        right_side_layout.Add(self.openpdf_button, 5, wx.LEFT | wx.RIGHT | wx.EXPAND, 5)
        right_side_layout.Add(
            self.openpage_button, 5, wx.LEFT | wx.RIGHT | wx.EXPAND, 5
        )
        layout = wx.BoxSizer(wx.HORIZONTAL)
        layout.Add(self.data_list, 30, wx.ALL | wx.EXPAND, 5)
        layout.Add(right_side_layout, 10, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.get_part_data()

    def quit_dialog(self, *_):
        """Close the dialog."""
        self.Destroy()
        self.EndModal(0)

    def savepdf(self, *_):
        """Download a datasheet from The LCSC API."""
        if self.pdfurl is not None:
            filename = self.pdfurl.rsplit("/", maxsplit=1)[1]
            self.logger.info("Save datasheet %s to %s", filename, self.datasheet_path)
            self.datasheet_path.mkdir(parents=True, exist_ok=True)
            result = self.lcsc_api.download_datasheet(
                self.pdfurl, self.datasheet_path / filename
            )
            title = "Success" if result["success"] else "Error"
            style = "info" if result["success"] else "error"
            resultMsg = result["msg"]
        else:
            title = "Error"
            style = "error"
            resultMsg = "Undefined URL for datasheet download"
        wx.PostEvent(
            self.parent,
            MessageEvent(
                title=title,
                text=resultMsg,
                style=style,
            ),
        )

    def openpdf(self, *_):
        """Open the linked datasheet PDF on button click."""
        self.logger.info("opening %s", str(self.pdfurl))
        webbrowser.open(str(self.pdfurl))

    def openpage(self, *_):
        """Open the linked LCSC page for the part on button click."""
        self.logger.info("opening LCSC page for %s", str(self.part))
        webbrowser.open(str(self.pageurl))

    def get_scaled_bitmap(self, url, width, height):
        """Download a picture from a URL and convert it into a wx Bitmap."""
        io_bytes = self.lcsc_api.download_bitmap(url)
        image = wx.Image(io_bytes)
        image = image.Scale(width, height, wx.IMAGE_QUALITY_HIGH)
        result = wx.Bitmap(image)
        return result

    def get_part_data(self):
        """Get part data from JLCPCB API and parse it into the table, set picture and PDF link."""
        result = self.lcsc_api.get_part_data(self.part)
        if not result["success"]:
            self.report_part_data_fetch_error(result["msg"])
            return

        parameters = {
            "componentCode": "Component Code",
            "firstTypeNameEn": "Primary Category",
            "secondTypeNameEn": "Secondary Category",
            "componentBrandEn": "Brand",
            "componentName": "Full Name",
            "componentDesignator": "Designator",
            "componentModelEn": "Model",
            "componentSpecificationEn": "Specification",
            "assemblyProcess": "Assembly Process",
            "describe": "Description",
            "matchedPartDetail": "Details",
            "stockCount": "Stock",
            "leastNumber": "Minimal Quantity",
            "leastNumberPrice": "Minimum price",
        }
        parttype = result["data"].get("data", {}).get("componentLibraryType")
        if parttype and parttype == "base":
            self.data_list.AppendItem(["Type", "Basic"])
        elif parttype and parttype == "expand":
            self.data_list.AppendItem(["Type", "Extended"])
        for k, v in parameters.items():
            val = result["data"].get("data", {}).get(k)
            if val:
                self.data_list.AppendItem([v, str(val)])
        prices = result["data"].get("data", {}).get("jlcPrices", [])
        if prices:
            for price in prices:
                start = price.get("startNumber")
                end = price.get("endNumber")
                if end == -1:
                    self.data_list.AppendItem(
                        [
                            f"JLC Price for >{start}",
                            str(price.get("productPrice")),
                        ]
                    )
                else:
                    self.data_list.AppendItem(
                        [
                            f"JLC Price for {start}-{end}",
                            str(price.get("productPrice")),
                        ]
                    )
        prices = result["data"].get("data", {}).get("prices", [])
        if prices:
            for price in prices:
                start = price.get("startNumber")
                end = price.get("endNumber")
                if end == -1:
                    self.data_list.AppendItem(
                        [
                            f"LCSC Price for >{start}",
                            str(price.get("productPrice")),
                        ]
                    )
                else:
                    self.data_list.AppendItem(
                        [
                            f"LCSC Price for {start}-{end}",
                            str(price.get("productPrice")),
                        ]
                    )
        for attribute in result["data"].get("data", {}).get("attributes", []):
            self.data_list.AppendItem(
                [
                    attribute.get("attribute_name_en"),
                    str(attribute.get("attribute_value_name")),
                ]
            )
        picture = result["data"].get("data", {}).get("minImage")
        if picture:
            # get the full resolution image instead of the thumbnail
            picture = picture.replace("96x96", "900x900")
        else:
            imageId = result["data"].get("data", {}).get("productBigImageAccessId")
            picture = (
                f"https://jlcpcb.com/api/file/downloadByFileSystemAccessId/{imageId}"
            )
        self.image.SetBitmap(
            self.get_scaled_bitmap(
                picture,
                int(200 * self.parent.scale_factor),
                int(200 * self.parent.scale_factor),
            )
        )

        self.pdfurl = result["data"].get("data", {}).get("dataManualUrl")
        self.pageurl = result["data"].get("data", {}).get("lcscGoodsUrl")

    def report_part_data_fetch_error(self, reason):
        """Spawn a message box with an erro message if the fetch fails."""
        wx.MessageBox(
            f"Failed to download part detail from the JLCPCB API ({reason})\r\n"
            f"We looked for a part named:\r\n{self.part}\r\n[hint: did you fill in the LCSC field correctly?]",
            "Error",
            style=wx.ICON_ERROR,
        )
        self.EndModal(-1)
