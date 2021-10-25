import io
import logging
import webbrowser

import requests
import wx


class PartDetailsDialog(wx.Dialog):
    def __init__(self, parent, part):
        self.part = part
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB Part Details",
            pos=wx.DefaultPosition,
            size=wx.Size(800, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )
        # This panel is unused, but without it the acceleraors don't work (on MacOS at least)
        self.panel = wx.Panel(parent=self, id=wx.ID_ANY)
        self.panel.Fit()

        quitid = wx.NewIdRef()
        aTable = wx.AcceleratorTable(
            [
                (wx.ACCEL_CTRL, ord("W"), quitid),
                (wx.ACCEL_CTRL, ord("Q"), quitid),
                (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, quitid),
            ]
        )
        self.SetAcceleratorTable(aTable)
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)
        self.logger = logging.getLogger(__name__)

        layout = wx.BoxSizer(wx.HORIZONTAL)

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
            width=200,
            align=wx.ALIGN_LEFT,
        )
        self.value = self.data_list.AppendTextColumn(
            "Value",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=300,
            align=wx.ALIGN_LEFT,
        )
        layout.Add(self.data_list, 20, wx.ALL | wx.EXPAND, 5)

        self.get_part_data()

        rhslayout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(rhslayout, 20, wx.ALL | wx.EXPAND, 5)

        if self.picture:
            staticImage = wx.StaticBitmap(
                self,
                wx.ID_ANY,
                self.get_scaled_bitmap(self.picture, 200, 200),
                wx.DefaultPosition,
                (200, 200),
                0,
            )
            rhslayout.Add(staticImage, 10, wx.ALL | wx.EXPAND, 5)

        if self.pdfurl:
            openpdf = wx.Button(
                self,
                wx.ID_ANY,
                "Open Datasheet",
                wx.DefaultPosition,
                wx.DefaultSize,
                0,
            )
            openpdf.Bind(wx.EVT_BUTTON, self.openpdf)
            rhslayout.AddStretchSpacer(50)
            rhslayout.Add(openpdf, 10, wx.LEFT | wx.RIGHT | wx.EXPAND, 5)
        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def openpdf(self, e):
        self.logger.info("opening %s", str(self.pdfurl))
        webbrowser.open(self.pdfurl)

    def get_scaled_bitmap(self, url, width, height):
        content = requests.get(url).content
        io_bytes = io.BytesIO(content)
        image = wx.Image(io_bytes)
        image = image.Scale(width, height, wx.IMAGE_QUALITY_HIGH)
        result = wx.Bitmap(image)
        return result

    def get_part_data(self):
        r = requests.get(
            f"https://wwwapi.lcsc.com/v1/products/detail?product_code={self.part}"
        )
        parameters = {
            "productCode": "Product Code",
            "productModel": "Model",
            "parentCatalogName": "Main Category",
            "catalogName": "Sub Category",
            "brandNameEn": "Brand",
            "encapStandard": "Package",
            "productUnit": "Unit",
            "productWeight": "Weight",
            "pdfUrl": "Data Sheet",
        }
        data = r.json()
        for k, v in parameters.items():
            val = data.get(k)
            if k == "pdfUrl":
                self.pdfurl = val
            if val:
                self.data_list.AppendItem([v, str(val)])
        if data.get("paramVOList"):
            for item in data.get("paramVOList", []):
                self.data_list.AppendItem(
                    [item["paramNameEn"], str(item["paramValueEn"])]
                )
        if len(data["productImages"]) > 0:
            self.picture = data["productImages"][0]
        else:
            self.picture = None
