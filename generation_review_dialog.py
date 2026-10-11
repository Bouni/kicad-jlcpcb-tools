"""Review pane listing every generation finding for one decision."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

# pyright: reportMissingImports=false, reportMissingModuleSource=false
import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error

from .generation_review import (
    HEADINGS,
    Finding,
    Severity,
    detail_text,
    group_findings,
    summary_text,
)
from .helpers import HighResWxSize

NO_SELECTION = "Select a finding to see its detail."
_ICONS = {
    Severity.ERROR: wx.ART_ERROR,
    Severity.WARNING: wx.ART_WARNING,
    Severity.INFO: wx.ART_INFORMATION,
}


class _FindingStore(dv.DataViewListStore):
    """Show the severity heading rows in bold."""

    def __init__(self) -> None:
        dv.DataViewListStore.__init__(self)
        self.headings: set[int] = set()

    def GetAttrByRow(self, row: int, col: int, attr: dv.DataViewItemAttr) -> bool:
        if row not in self.headings:
            return False
        attr.SetBold(True)
        return True


class GenerationReviewDialog(wx.Dialog):
    """List findings by severity; ShowModal returns wx.ID_OK to generate anyway.

    Selecting a finding shows its detail, and the parts it names, below the list.
    """

    def __init__(self, parent: Any, findings: Sequence[Finding]) -> None:
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="Review before generating",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._rows: list[Optional[Finding]] = []

        self.summary = wx.StaticText(self, wx.ID_ANY, summary_text(findings))
        self.findings_list = dv.DataViewListCtrl(
            self, wx.ID_ANY, style=dv.DV_SINGLE | dv.DV_ROW_LINES
        )
        self._store = _FindingStore()
        self.findings_list.AssociateModel(self._store)
        self.findings_list.AppendIconTextColumn(
            "Check",
            width=HighResWxSize(self, wx.Size(190, -1)).GetWidth(),
            mode=dv.DATAVIEW_CELL_INERT,
        )
        self.findings_list.AppendTextColumn(
            "Finding",
            width=HighResWxSize(self, wx.Size(480, -1)).GetWidth(),
            mode=dv.DATAVIEW_CELL_INERT,
        )
        for severity, group in group_findings(findings):
            icon = wx.ArtProvider.GetIcon(_ICONS[severity], wx.ART_LIST)
            # Headings carry their group's icon as well: under wxGTK, rows
            # without an icon at the top can leave no room for those below.
            self._store.headings.add(len(self._rows))
            self._append(dv.DataViewIconText(HEADINGS[severity], icon), "", None)
            for finding in group:
                self._append(
                    dv.DataViewIconText(finding.check, icon), finding.title, finding
                )

        self.detail = wx.TextCtrl(
            self, wx.ID_ANY, NO_SELECTION, style=wx.TE_MULTILINE | wx.TE_READONLY
        )

        # wxPython does not wrap StdDialogButtonSizer.GetAffirmativeButton, so
        # the sizer is given buttons created here.
        self.generate_button = wx.Button(self, wx.ID_OK, "Generate anyway")
        self.cancel_button = wx.Button(self, wx.ID_CANCEL)
        self.cancel_button.SetDefault()
        self.SetEscapeId(wx.ID_CANCEL)
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(self.generate_button)
        buttons.AddButton(self.cancel_button)
        buttons.Realize()

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.summary, 0, wx.ALL, 10)
        layout.Add(self.findings_list, 2, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
        layout.Add(self.detail, 1, wx.ALL | wx.EXPAND, 10)
        layout.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        self.SetSizer(layout)
        self.SetMinSize(HighResWxSize(self, wx.Size(480, 320)))
        self.SetSize(HighResWxSize(self, wx.Size(720, 420)))

        self.findings_list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_selection)
        # Start on Cancel, the default; wxGTK would otherwise select the first
        # row as the list takes the focus.
        self.cancel_button.SetFocus()
        self.CentreOnParent()

    def _append(self, check: Any, title: str, finding: Optional[Finding]) -> None:
        """Add one list row; a heading row has no finding."""
        self.findings_list.AppendItem([check, title])
        self._rows.append(finding)

    def _on_selection(self, _event: Any) -> None:
        """Show the selected finding's detail."""
        row = self.findings_list.GetSelectedRow()
        finding = self._rows[row] if row != wx.NOT_FOUND else None
        self.detail.SetValue(detail_text(finding) if finding else NO_SELECTION)
