"""Modal prompt for typing or pasting one LCSC part number."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

# pyright: reportMissingImports=false, reportMissingModuleSource=false
import wx  # pylint: disable=import-error

from .helpers import HighResWxSize
from .lcsc import parse_lcsc_entry

if TYPE_CHECKING:
    from .mainwindow import JLCPCBTools

ENTRY_HINT = "Type a code like C25804, or paste an LCSC or JLCPCB product link."
_TITLE_REFERENCES = 5


def entry_title(references: Sequence[str]) -> str:
    """Name the parts being assigned, shortening a long selection."""
    shown = ", ".join(references[:_TITLE_REFERENCES])
    hidden = len(references) - _TITLE_REFERENCES
    return f"Enter LCSC for {shown}" + (f" and {hidden} more" if hidden > 0 else "")


class LcscEntryDialog(wx.Dialog):
    """Ask for one LCSC number; OK stays disabled until the text names one."""

    def __init__(
        self, parent: JLCPCBTools, references: Sequence[str], initial: str = ""
    ) -> None:
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=entry_title(references),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self.code = ""
        prompt = wx.StaticText(self, wx.ID_ANY, "LCSC part number or product link")
        self.text = wx.TextCtrl(
            self,
            wx.ID_ANY,
            initial,
            size=HighResWxSize(parent.window, wx.Size(360, -1)),
            style=wx.TE_PROCESS_ENTER,
        )
        # Reserve the longer hint's width: wxGTK fits the dialog to its sizer
        # again on Show, when a prefilled number already shows the shorter one.
        self.hint = wx.StaticText(self, wx.ID_ANY, ENTRY_HINT)
        self.hint.SetMinSize(self.hint.GetBestSize())
        # wxPython does not wrap StdDialogButtonSizer.GetAffirmativeButton, so
        # keep the OK button from creating it.
        self.ok_button = wx.Button(self, wx.ID_OK)
        self.ok_button.SetDefault()
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(self.ok_button)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(prompt, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        layout.Add(self.text, 0, wx.ALL | wx.EXPAND, 10)
        layout.Add(self.hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        layout.Add(buttons, 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(layout)

        self.text.Bind(wx.EVT_TEXT, self._on_text)
        self.text.Bind(wx.EVT_TEXT_ENTER, self._on_enter)
        self._on_text()
        self.text.SelectAll()
        self.text.SetFocus()
        self.CentreOnParent()

    def _on_text(self, *_: Any) -> None:
        """Show what OK would assign, and allow it only for exactly one number."""
        self.code = parse_lcsc_entry(self.text.GetValue())
        self.ok_button.Enable(bool(self.code))
        self.hint.SetLabel(f"Will assign {self.code}" if self.code else ENTRY_HINT)
        self.Layout()

    def _on_enter(self, *_: Any) -> None:
        """Accept on Enter only when OK would be enabled."""
        if self.code:
            self.EndModal(wx.ID_OK)
