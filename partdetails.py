import io
import logging
import webbrowser

import requests
import wx

from .helpers import HighResWxSize, loadBitmapScaled


class PartDetailsDialog(wx.Dialog):
    def __init__(self, parent, part):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB Part Details",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(1000, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.part = part
        self.pdfurl = None
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
        self.openpdf_button = wx.Button(
            self,
            wx.ID_ANY,
            "Open Datasheet",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.openpdf_button.Bind(wx.EVT_BUTTON, self.openpdf)

        self.openpdf_button.SetBitmap(
            loadBitmapScaled(
                "mdi-file-document-outline.png",
                self.parent.scale_factor,
            )
        )
        self.openpdf_button.SetBitmapMargins((2, 0))

        # ---------------------------------------------------------------------
        # ------------------------ Layout and Sizers --------------------------
        # ---------------------------------------------------------------------

        right_side_layout = wx.BoxSizer(wx.VERTICAL)
        right_side_layout.Add(self.image, 10, wx.ALL | wx.EXPAND, 5)
        right_side_layout.AddStretchSpacer(50)
        right_side_layout.Add(self.openpdf_button, 5, wx.LEFT | wx.RIGHT | wx.EXPAND, 5)
        layout = wx.BoxSizer(wx.HORIZONTAL)
        layout.Add(self.data_list, 30, wx.ALL | wx.EXPAND, 5)
        layout.Add(right_side_layout, 10, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.get_part_data()

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def openpdf(self, e):
        """Open the linked datasheet PDF on button click."""
        self.logger.info("opening %s", str(self.pdfurl))
        webbrowser.open(self.pdfurl)

    def get_scaled_bitmap(self, url, width, height):
        """Download a picture from a URL and convert it into a wx Bitmap"""
        content = requests.get(url).content
        io_bytes = io.BytesIO(content)
        image = wx.Image(io_bytes)
        image = image.Scale(width, height, wx.IMAGE_QUALITY_HIGH)
        result = wx.Bitmap(image)
        return result

    def get_part_data(self):
        """fetch part data from JLCPCB API and parse it into the table, set picture and PDF link"""
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36",
        }
        r = requests.get(
            f"https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={self.part}",
            headers=headers,
        )
        if r.status_code != requests.codes.ok:
            self.report_part_data_fetch_error("non-OK HTTP response status")

        data = r.json()
        if not data.get("data"):
            self.report_part_data_fetch_error(
                "returned JSON data does not have expected 'data' attribute"
            )

        parameters = {
            "componentCode": "Component code",
            "firstTypeNameEn": "Primary category",
            "secondTypeNameEn": "Secondary category",
            "componentBrandEn": "Brand",
            "componentName": "Full name",
            "componentDesignator": "Designator",
            "componentModelEn": "Model",
            "componentSpecificationEn": "Specification",
            "describe": "Description",
            "matchedPartDetail": "Details",
            "stockCount": "Stock",
            "leastNumber": "Minimal Quantity",
            "leastNumberPrice": "Minimum price",
        }
        parttype = data.get("data", {}).get("componentLibraryType")
        if parttype and parttype == "base":
            self.data_list.AppendItem(["Type", "Basic"])
        elif parttype and parttype == "expand":
            self.data_list.AppendItem(["Type", "Extended"])
        for k, v in parameters.items():
            val = data.get("data", {}).get(k)
            if val:
                self.data_list.AppendItem([v, str(val)])
        prices = data.get("data", {}).get("jlcPrices", [])
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
        prices = data.get("data", {}).get("prices", [])
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
        for attribute in data.get("data", {}).get("attributes", []):
            self.data_list.AppendItem(
                [
                    attribute.get("attribute_name_en"),
                    str(attribute.get("attribute_value_name")),
                ]
            )
        picture = data.get("data", {}).get("minImage")
        if picture:
            # get the full resolution image instead of the thumbnail
            picture = picture.replace("96x96", "900x900")
            self.image.SetBitmap(
                self.get_scaled_bitmap(
                    picture,
                    int(200 * self.parent.scale_factor),
                    int(200 * self.parent.scale_factor),
                )
            )
        self.pdfurl = data.get("data", {}).get("dataManualUrl")

    def report_part_data_fetch_error(self, reason):
        wx.MessageBox(
            f"Failed to download part detail from the JLCPCB API ({reason})\r\n"
            f"We looked for a part named:\r\n{self.part}\r\n[hint: did you fill in the LCSC field correctly?]",
            "Error",
            style=wx.ICON_ERROR,
        )
        self.EndModal(-1)
