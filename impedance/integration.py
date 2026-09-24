"""Connect board configuration and the impedance dialog to the main toolbar."""

from collections import OrderedDict
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any, Optional

import wx

from .catalog_cache import CatalogCache
from .database import ImpedanceDatabase
from .model import BoardSnapshot, Config, Section, ValidationError
from .palette import read_stackup_copper_colors, read_theme_context
from .repository import ImpedanceRepository
from .service import CapturedImage, ExportPlan, prepare, validate_current


class CaptureSession:
    """Own one board/appearance revision and its bounded native capture cache."""

    def __init__(self, controls: "ImpedanceControls", directory: Path) -> None:
        self.controls = controls
        self.directory = directory
        self.revision: Optional[tuple[BoardSnapshot, object]] = None
        self.renderer: Any = None
        self.images: OrderedDict[Section, CapturedImage] = OrderedDict()
        self.image_bytes = 0

    def close(self) -> None:
        """Release native resources and all captures together, including on failure."""
        renderer, self.renderer = self.renderer, None
        self.images.clear()
        self.image_bytes = 0
        self.revision = None
        if renderer is not None:
            renderer.close()

    def snapshot(self) -> BoardSnapshot:
        """Check the live board identity and invalidate an obsolete capture revision."""
        try:
            current = self.controls._snapshot()
            if self.revision is not None and current != self.revision[0]:
                self.close()
            return current
        except Exception:
            self.close()
            raise

    def verify(self, expected: BoardSnapshot) -> None:
        """Reject a changed source without adopting it in a nested editor."""
        if self.snapshot() != expected:
            raise ValidationError(
                "The board changed. Review the updated workbook rows before approval."
            )

    def preview(self, section: Section, *, refresh: bool = False) -> CapturedImage:
        """Reuse plots and PNGs only while their complete source revision is current."""
        from .render import SectionRenderer

        pcbnew = self.controls.parent.pcbnew
        try:
            revision = (self.snapshot(), read_theme_context(pcbnew))
            if revision != self.revision:
                self.close()
                self.renderer = SectionRenderer(pcbnew.GetBoard(), pcbnew)
                self.revision = revision
            if refresh:
                previous = self.images.pop(section, None)
                if previous is not None:
                    self.image_bytes -= len(previous.data)
            cached = self.images.get(section)
            if cached is not None:
                self.images.move_to_end(section)
                return cached
            path = self.renderer.render(
                section, self.directory / f"{section.section_id}.png"
            )
            capture = CapturedImage.load(path)
            if (self.snapshot(), read_theme_context(pcbnew)) != revision:
                raise ValidationError(
                    "The board or palette changed during capture. Review the updated images."
                )
            while (
                self.images and self.image_bytes + len(capture.data) > 64 * 1024 * 1024
            ):
                _, evicted = self.images.popitem(last=False)
                self.image_bytes -= len(evicted.data)
            self.images[section] = capture
            self.image_bytes += len(capture.data)
            return capture
        except Exception:
            self.close()
            raise


class ImpedanceControls:
    """Own the toolbar state and transactional board configuration workflow."""

    def __init__(self, parent: Any, toolbar: Any) -> None:
        self.parent = parent
        self.board_path = str(parent.pcbnew.GetBoard().GetFileName())
        self.database: Optional[ImpedanceDatabase] = None
        self.repository: Optional[ImpedanceRepository] = None
        self.config = Config()
        self.revision = 0
        self.error = ""
        self._export_revision = 0
        self.checkbox = wx.CheckBox(toolbar, label="Controlled impedance")
        self.configure_button = wx.Button(toolbar, label="Configure…")
        self.status = wx.StaticText(toolbar, label="")
        self.checkbox.SetToolTip("Include the JLCPCB impedance form in the Gerber ZIP")
        for control in (self.checkbox, self.configure_button, self.status):
            toolbar.AddControl(control)
        self.checkbox.Enable(False)
        self.configure_button.Enable(False)
        self.checkbox.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        self.configure_button.Bind(wx.EVT_BUTTON, self.on_configure)

    def attach_store(self, store: Any) -> None:
        """Restore feature settings without creating storage or registering a PCB."""
        self.database = None
        self.repository = None
        try:
            ImpedanceDatabase.validate_board_path(self.board_path)
            self.database = ImpedanceDatabase(store.dbfile, initialize=False)
            self.checkbox.Enable(True)
            self.configure_button.Enable(True)
            try:
                self.reload()
            except ValidationError as exc:
                self.error = str(exc)
                self.status.SetLabel("Settings need attention")
                self.status.SetToolTip(self.error)
                self.checkbox.SetValue(True)
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            self.database = None
            self.repository = None
            self.error = str(exc)
            self.checkbox.Enable(False)
            self.configure_button.Enable(False)
            self.status.SetLabel("Impedance settings unavailable")
            self.status.SetToolTip(self.error)

    def check_board(self) -> None:
        """Stop an open window from writing to the prior board after Save As."""
        current = str(self.parent.pcbnew.GetBoard().GetFileName())
        if not current or Path(current).resolve() != Path(self.board_path).resolve():
            raise ValidationError(
                "The PCB filename changed. Reopen JLCPCB Tools for the current board."
            )
        if self.repository is not None:
            self.repository.database.ensure_current_board(
                self.repository.board_id, current
            )

    def reload(self) -> Config:
        """Read a fresh database revision, retaining errors for export preflight."""
        self.check_board()
        if self.repository is None:
            if self.database is None:
                raise ValidationError(
                    self.error or "The impedance database is not ready."
                )
            board_id = self.database.find_board(self.board_path)
            if board_id is not None:
                self.repository = ImpedanceRepository(self.database, board_id)
        if self.repository is None:
            self.config, self.revision = Config(), 0
        else:
            self.config, self.revision = self.repository.load()
        self.error = ""
        self._update_saved_status()
        return self.config

    def _update_saved_status(self) -> None:
        """Show stored intent without claiming its approval matches the live PCB."""
        self.checkbox.SetValue(self.config.enabled)
        count = len(self.config.included_section_ids)
        specs = len(self.config.specifications)
        if self.config.reviewed_digest and count:
            label = f"{specs} specs · {count} sections with saved approval"
        elif self.config.enabled or specs or self.config.stackup is not None:
            label = f"{specs} specs · Needs review"
        else:
            label = ""
        self.status.SetLabel(label)
        self.status.SetToolTip("")

    def _snapshot(self) -> BoardSnapshot:
        """Extract a fresh immutable snapshot using the active KiCad adapter."""
        from .pcbnew_adapter import snapshot_board

        self.check_board()
        return snapshot_board(self.parent.pcbnew.GetBoard(), self.parent.pcbnew)

    def _save(self, config: Config) -> None:
        """Commit checked intent without postcommit database or native UI calls."""
        revision = self._writable_repository().save(config, self.revision)
        # The save already validated the payload and returned its committed
        # revision. A second read could fail after a successful commit or adopt
        # another editor's newer write, incorrectly reporting this save's state.
        self.config = config
        self.revision = revision
        self.error = ""

    def _writable_repository(self) -> ImpedanceRepository:
        """Register a PCB only for an explicit settings or catalog write."""
        self.check_board()
        if self.repository is None:
            if self.database is None:
                raise ValidationError("The project database is not ready.")
            self.database.initialize()
            board_id = self.database.resolve_board(self.board_path)
            self.repository = ImpedanceRepository(self.database, board_id)
        return self.repository

    def _show_error(self, exc: Exception) -> None:
        """Display an actionable configuration or rendering failure."""
        wx.MessageBox(
            str(exc), "Controlled impedance", wx.OK | wx.ICON_ERROR, self.parent
        )

    def on_toggle(self, _event: Any) -> None:
        """Enable reviewed settings or edit an enabled draft; retain specs on disable."""
        requested = self.checkbox.GetValue()
        try:
            config = self.reload()
            if not requested:
                self._save(replace(config, enabled=False))
                self._update_saved_status()
                return
            proposed = replace(config, enabled=True)
            try:
                prepare(proposed, self._snapshot())
            except ValidationError:
                self.on_configure(None, requested_enabled=True)
                return
            self._save(proposed)
            self._update_saved_status()
        except Exception as exc:
            self.checkbox.SetValue(self.config.enabled)
            self._show_error(exc)

    def on_configure(
        self, _event: Any, *, requested_enabled: Optional[bool] = None
    ) -> None:
        """Edit a working copy and commit only from the still-open dialog's Save."""
        from .dialog import ImpedanceDialog

        try:
            try:
                config = self.reload()
            except ValidationError as exc:
                if not self._reset_config(exc):
                    return
                config = self.reload()
            if requested_enabled is not None:
                config = replace(config, enabled=requested_enabled)
            self.check_board()
            if not self.parent.prepare_copper_zones(for_review=True):
                self.checkbox.SetValue(self.config.enabled)
                return
            snapshot = self._snapshot()
            with (
                TemporaryDirectory(prefix="jlcpcb-impedance-preview-") as scratch,
                closing(CaptureSession(self, Path(scratch))) as captures,
            ):

                def load_stackup_catalog(layer_count: int) -> CatalogCache:
                    """Read reusable vendor rows and check time without altering intent."""
                    self.check_board()
                    if self.database is None:
                        raise ValidationError("The project database is not ready.")
                    return ImpedanceRepository.read_stackup_catalog(
                        self.database, layer_count
                    )

                def save_stackup_catalog(layer_count: int, cache: CatalogCache) -> None:
                    """Persist successful automatic checks, never the board selection."""
                    self._writable_repository().save_stackup_catalog(layer_count, cache)

                dialog = ImpedanceDialog(
                    self.parent,
                    config,
                    snapshot,
                    captures.preview,
                    captures.snapshot,
                    verify_snapshot=captures.verify,
                    appearance_context=lambda: read_theme_context(self.parent.pcbnew),
                    load_stackup_catalog=load_stackup_catalog,
                    save_stackup_catalog=save_stackup_catalog,
                    load_stackup_colors=lambda: read_stackup_copper_colors(
                        self.parent.pcbnew
                    ),
                    save_config=self._save,
                )
                try:
                    if dialog.ShowModal() == wx.ID_OK:
                        self._update_saved_status()
                    else:
                        self.checkbox.SetValue(self.config.enabled)
                finally:
                    dialog.Destroy()
        except Exception as exc:
            self.checkbox.SetValue(self.config.enabled)
            self._show_error(exc)

    def _reset_config(self, error: ValidationError) -> bool:
        """Offer a confirmed reset of invalid settings without saving a history."""
        self.check_board()
        if self.repository is None:
            raise error
        database = self.repository.database
        board_id = self.repository.board_id
        token = database.config_reset_token(board_id)
        try:
            self.reload()
        except ValidationError as current_error:
            error = current_error
        else:
            # Another window repaired the row before the reset token was read.
            return True
        dialog = wx.MessageDialog(
            self.parent,
            f"{error}\n\nReset controlled-impedance settings for this board? "
            "The existing impedance configuration will be replaced with disabled defaults. "
            "This cannot be undone.",
            "Reset controlled-impedance settings",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        try:
            dialog.SetYesNoLabels("Reset settings", "Cancel")
            if dialog.ShowModal() != wx.ID_YES:
                return False
        finally:
            dialog.Destroy()
        self.check_board()
        database.reset_config(board_id, token, Config().to_dict())
        self.reload()
        return True

    def preflight(self, layer_count: Optional[int]) -> Optional[ExportPlan]:
        """Validate enabled settings before any fabrication side effects."""
        config = self.reload()
        self._export_revision = self.revision
        if not config.enabled:
            return None
        return prepare(config, self._snapshot(), layer_count)

    def verify_current(self, plan: ExportPlan, layer_count: Optional[int]) -> None:
        """Reject edits made during hooks, zone checks, or rendering."""
        config = self.reload()
        validate_current(plan, config, self._snapshot(), layer_count)

    def verify_disabled(self) -> None:
        """Prevent a concurrent enable/save from producing a ZIP without its form."""
        config = self.reload()
        if config.enabled or self.revision != self._export_revision:
            raise ValidationError(
                "Controlled-impedance settings changed during generation. Generate again."
            )
