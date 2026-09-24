"""Connect board configuration and the impedance dialog to the main toolbar."""

from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any, Optional

import wx

from .board_copy import NativeBoardOwner
from .catalog_cache import CatalogCache
from .database import DatabaseMigrationError, ImpedanceDatabase
from .model import BoardSnapshot, Config, Section, ValidationError
from .palette import read_stackup_copper_colors, read_theme_context
from .repository import ImpedanceRepository
from .service import CapturedImage, ExportPlan, prepare, validate_current


class ImpedanceStorageError(RuntimeError):
    """Saved impedance intent cannot currently be read safely."""


@dataclass(frozen=True)
class PreparedSource:
    """Retain the initial prepared-source snapshot or its failure before modal UI."""

    revision: Optional[tuple[BoardSnapshot, Optional[str]]] = None
    error: Optional[Exception] = None


class OutputBoardSession:
    """Keep a selected-variant review copy only while its live source is current."""

    def __init__(
        self, controls: "ImpedanceControls", invalidate: Callable[[], None]
    ) -> None:
        self.controls = controls
        self.invalidate = invalidate
        self.owner: Optional[NativeBoardOwner] = None
        self.revision: Optional[tuple[BoardSnapshot, Optional[str]]] = None
        self.current: Optional[BoardSnapshot] = None

    def close(self) -> None:
        """Release dependent renderers before their source board allocation."""
        owner, self.owner = self.owner, None
        self.revision = None
        self.current = None
        try:
            self.invalidate()
        finally:
            if owner is not None:
                owner.close()

    @property
    def board(self) -> Any:
        """Return the still-owned selected output board for preview rendering."""
        if self.owner is None:
            raise ValidationError("The output-variant review board is not ready.")
        return self.owner.board

    def snapshot(self) -> BoardSnapshot:
        """Reuse a copy only after freshly checking live geometry and output selection."""
        revision = self.controls._source_revision()
        if revision != self.revision:
            self.close()
            source, variant = revision
            if variant is None:
                return source
            owner = self.controls.parent.fabrication.copy_variant_board(variant)
            try:
                current = self.controls.snapshot_output_board(owner.board)
                if self.controls._source_revision() != revision:
                    raise ValidationError(
                        "The board or output variant changed while preparing review. "
                        "Review the current output before approval."
                    )
                self.owner, self.revision, self.current = owner, revision, current
            except BaseException:
                owner.close()
                raise
        assert self.current is not None
        return self.current


class CaptureSession:
    """Own one board/appearance revision and its bounded native capture cache."""

    def __init__(self, controls: "ImpedanceControls", directory: Path) -> None:
        self.controls = controls
        self.directory = directory
        self.revision: Optional[tuple[BoardSnapshot, object]] = None
        self.renderer: Any = None
        self.images: OrderedDict[Section, CapturedImage] = OrderedDict()
        self.image_bytes = 0
        self.output = (
            OutputBoardSession(controls, self._close_captures)
            if controls._selected_output_variant() is not None
            else None
        )

    def _close_captures(self) -> None:
        """Release renderers before replacing a detached output source."""
        renderer, self.renderer = self.renderer, None
        self.images.clear()
        self.image_bytes = 0
        self.revision = None
        if renderer is not None:
            renderer.close()

    def close(self) -> None:
        """Release native resources and all captures together, including on failure."""
        if self.output is None:
            self._close_captures()
        else:
            self.output.close()

    def snapshot(self) -> BoardSnapshot:
        """Check the live board identity and invalidate an obsolete capture revision."""
        try:
            current = (
                self.controls._snapshot()
                if self.output is None
                else self.output.snapshot()
            )
            if self.revision is not None and current != self.revision[0]:
                self._close_captures()
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
                self._close_captures()
                board = pcbnew.GetBoard() if self.output is None else self.output.board
                self.renderer = SectionRenderer(board, pcbnew)
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
        self._store: Any = None
        self._storage_error: Optional[ImpedanceStorageError] = None
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
        self._store = store
        self._storage_error = None
        try:
            ImpedanceDatabase.validate_board_path(self.board_path)
        except (ValueError, OSError) as exc:
            self.error = str(exc)
            self.checkbox.Enable(False)
            self.configure_button.Enable(False)
            self.status.SetLabel("Impedance settings unavailable")
            self.status.SetToolTip(self.error)
            return
        try:
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
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            self._unavailable_storage(exc)
        except ValueError as exc:
            self.error = str(exc)
            self.checkbox.Enable(False)
            self.configure_button.Enable(False)
            self.status.SetLabel("PCB identity needs attention")
            self.status.SetToolTip(self.error)

    def _unavailable_storage(self, error: Exception) -> ImpedanceStorageError:
        """Keep identity and saved intent while leaving a recovery action reachable."""
        self._storage_error = ImpedanceStorageError(str(error))
        self.error = str(error)
        self.checkbox.Enable(False)
        self.configure_button.Enable(True)
        self.status.SetLabel("Impedance settings unavailable")
        self.status.SetToolTip(self.error)
        return self._storage_error

    def retry_storage(self) -> Config:
        """Retry reads without creating tables or replacing a known board identity."""
        if self.database is None and self._store is not None:
            self.check_board()
            ImpedanceDatabase.validate_board_path(self.board_path)
            try:
                self.database = ImpedanceDatabase(self._store.dbfile, initialize=False)
            except (RuntimeError, OSError, sqlite3.Error) as exc:
                raise self._unavailable_storage(exc) from exc
        return self.reload()

    def storage_error_action(
        self, error: ImpedanceStorageError, *, allow_omit: bool = False
    ) -> str:
        """Require an explicit selection for retry or one-time report omission."""
        choices = ["Cancel", "Retry reading impedance settings"]
        if allow_omit:
            choices.append(
                "Generate without impedance workbook and HTML report this time"
            )
        dialog = wx.SingleChoiceDialog(
            self.parent,
            f"Controlled-impedance settings could not be read:\n\n{error}\n\n"
            "Saved settings will be preserved. Choose how to continue.",
            "Impedance settings unavailable",
            choices,
        )
        try:
            dialog.SetSelection(0)
            if dialog.ShowModal() != wx.ID_OK:
                return "cancel"
            selection = dialog.GetSelection()
            if selection == 1:
                return "retry"
            if allow_omit and selection == 2:
                return "omit"
            return "cancel"
        finally:
            dialog.Destroy()

    def check_board(self) -> None:
        """Stop an open window from writing to the prior board after Save As."""
        try:
            current = str(self.parent.pcbnew.GetBoard().GetFileName())
        except (RuntimeError, OSError, ValueError) as exc:
            raise ValidationError(
                f"The PCB identity is unavailable. Reopen JLCPCB Tools. {exc}"
            ) from exc
        if not current or Path(current).resolve() != Path(self.board_path).resolve():
            raise ValidationError(
                "The PCB filename changed. Reopen JLCPCB Tools for the current board."
            )
        if self.repository is not None:
            try:
                self.repository.database.ensure_current_board(
                    self.repository.board_id, current
                )
            except (DatabaseMigrationError, OSError, sqlite3.Error) as exc:
                raise self._unavailable_storage(exc) from exc

    def reload(self) -> Config:
        """Read a fresh database revision, retaining errors for export preflight."""
        self.check_board()
        try:
            if self.repository is None:
                if self.database is None:
                    if self._storage_error is not None:
                        raise self._storage_error
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
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            raise self._unavailable_storage(exc) from exc
        self._storage_error = None
        self.error = ""
        self.checkbox.Enable(True)
        self.configure_button.Enable(True)
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
        """Review the selected fabrication output while leaving the editor untouched."""
        if self._selected_output_variant() is None:
            return self._live_snapshot()
        with closing(OutputBoardSession(self, lambda: None)) as output:
            return output.snapshot()

    def _live_snapshot(self) -> BoardSnapshot:
        """Read the source independently so edits invalidate cached review copies."""
        from .pcbnew_adapter import snapshot_board

        self.check_board()
        return snapshot_board(self.parent.pcbnew.GetBoard(), self.parent.pcbnew)

    def _selected_output_variant(self) -> Optional[str]:
        """Distinguish ordinary boards from an explicit Default variant output."""
        controller = getattr(self.parent, "_variant_controller", None)
        if controller is None:
            return None
        name = controller.session.output_variant
        if not isinstance(name, str):
            raise ValidationError("Select an available fabrication output variant.")
        return name

    def _source_revision(self) -> tuple[BoardSnapshot, Optional[str]]:
        """Bind a read-only source revision to the independently selected output."""
        get_board = getattr(self.parent, "_legacy_board", None)
        if callable(get_board):
            get_board()
        return self._live_snapshot(), self._selected_output_variant()

    def prepared_source_revision(self, board: Any) -> PreparedSource:
        """Bind the prepared source before allowing settings recovery or review."""
        from .pcbnew_adapter import snapshot_board

        self.check_board()
        variant = self._selected_output_variant()
        try:
            snapshot = snapshot_board(board, self.parent.pcbnew)
        except Exception as error:
            # Geometry support must not block an explicit reset of malformed
            # settings. Keep this failure, rather than adopting geometry edited
            # while the reset/recovery modal is open.
            return PreparedSource(error=error)
        return PreparedSource(revision=(snapshot, variant))

    def snapshot_output_board(self, board: Any) -> BoardSnapshot:
        """Fingerprint exact output geometry/text with live native class metadata."""
        from .pcbnew_adapter import snapshot_board

        self.check_board()
        name = self._selected_output_variant()
        if name is None:
            return snapshot_board(board, self.parent.pcbnew)
        if str(board.GetCurrentVariant()) != name:
            raise ValidationError(
                "The fabrication output variant changed. Review its current output."
            )
        snapshot = snapshot_board(
            board,
            self.parent.pcbnew,
            netclass_source=self.parent.pcbnew.GetBoard(),
        )
        context = json.dumps((snapshot.context_digest, name), ensure_ascii=True)
        return replace(
            snapshot,
            context_digest=hashlib.sha256(context.encode("ascii")).hexdigest(),
        )

    def verify_output_board(
        self, plan: ExportPlan, board: Any, layer_count: Optional[int]
    ) -> None:
        """Reject a report/plot board that differs from the approved output context."""
        validate_current(
            plan, plan.config, self.snapshot_output_board(board), layer_count
        )

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
        self,
        _event: Any,
        *,
        requested_enabled: Optional[bool] = None,
        prepared_source: Optional[PreparedSource] = None,
    ) -> bool:
        """Edit transactionally; return False when this generation must stop."""
        from .dialog import ImpedanceDialog

        try:
            while True:
                try:
                    config = self.reload()
                    break
                except ImpedanceStorageError as exc:
                    if self.storage_error_action(exc) != "retry":
                        return False
                    with suppress(ImpedanceStorageError, ValidationError):
                        self.retry_storage()
                except ValidationError as exc:
                    if not self._reset_config(exc):
                        return False
            if requested_enabled is not None:
                config = replace(config, enabled=requested_enabled)
            self.check_board()
            if prepared_source is None:
                if not self.parent.prepare_copper_zones():
                    self.checkbox.SetValue(self.config.enabled)
                    return False
            elif prepared_source.error is not None:
                raise prepared_source.error
            elif self._source_revision() != prepared_source.revision:
                raise ValidationError(
                    "The prepared board changed. Generate again before review."
                )
            with (
                TemporaryDirectory(prefix="jlcpcb-impedance-preview-") as scratch,
                closing(CaptureSession(self, Path(scratch))) as captures,
            ):
                snapshot = captures.snapshot()
                if (
                    prepared_source is not None
                    and self._source_revision() != prepared_source.revision
                ):
                    raise ValidationError(
                        "The prepared board changed while opening review. Generate again."
                    )

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
                    accepted = dialog.ShowModal() == wx.ID_OK
                    if (
                        prepared_source is not None
                        and self._source_revision() != prepared_source.revision
                    ):
                        raise ValidationError(
                            "The prepared board changed during configuration. "
                            "Generate again before exporting."
                        )
                    if accepted:
                        self._update_saved_status()
                    else:
                        self.checkbox.SetValue(self.config.enabled)
                    return accepted
                finally:
                    dialog.Destroy()
        except Exception as exc:
            self.checkbox.SetValue(self.config.enabled)
            self._show_error(exc)
            # An explicit reset can recover unavailable impedance geometry by
            # leaving reports disabled. A known source that changed, failed a
            # later freshness read, or was cancelled must restart Generate.
            return (
                prepared_source is not None
                and prepared_source.error is exc
                and not self.config.enabled
            )

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

    def verify_omission(self) -> None:
        """Retain native and known registry identity checks after explicit omission."""
        # Only reading feature storage was waived for this generation. A
        # changed/native-unavailable board still raises before this point.
        with suppress(ImpedanceStorageError):
            self.check_board()
