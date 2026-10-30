"""Connect board configuration and the impedance dialog to the main toolbar."""

from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, replace
import hashlib
from inspect import getattr_static
import json
import logging
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TYPE_CHECKING, Any, Optional, Union
from weakref import ReferenceType, ref

import wx

from .board_copy import NativeBoardOwner
from .calculator_config_cache import CalculatorConfigCache
from .capture_cache import (
    MAX_OWNED_CAPTURE_BYTES,
    CaptureKey,
    OwnedCaptureCache,
    capture_key,
)
from .catalog_cache import CatalogCache
from .database import DatabaseMigrationError, ImpedanceDatabase
from .model import BoardSnapshot, Config, Section, ValidationError
from .palette import read_stackup_copper_colors, read_theme_context
from .repository import ImpedanceRepository
from .service import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    CapturedImage,
    CaptureEvidence,
    ExportArtifacts,
    ExportPlan,
    SpacingCaptureEvidence,
    load_rendered_capture,
    load_spacing_capture,
    prepare,
    validate_current,
)
from .source_evidence import SourceSnapshotChanged, SourceSnapshotReader
from .stackup_model import WidthResult

if TYPE_CHECKING:
    from .spacing_review import SpacingReviewEntry

_LOG = logging.getLogger(__name__)
MAX_CAPTURE_CACHE_BYTES = 64 * 1024 * 1024
MAX_SPACING_CAPTURE_CACHE_BYTES = 64 * 1024 * 1024
CaptureRevision = tuple[BoardSnapshot, object, Optional[str], int]
_SOURCE_READER_POLICY = 1


def _source_board_key(
    board: Any, guard: Optional[Callable[[], None]] = None
) -> tuple[object, ...]:
    """Identify native allocation, UUID and path without retaining its wrapper."""
    if guard is not None:
        guard()
    if board is None:
        raise ValidationError("The PCB editor no longer has an open board.")
    pointer = board
    seen: set[int] = set()
    for _depth in range(8):
        if getattr_static(pointer, "this", None) is None or id(pointer) in seen:
            break
        seen.add(id(pointer))
        pointer = pointer.this
        if guard is not None:
            guard()
    address = id(board)
    if seen:
        with suppress(TypeError, ValueError):
            address = int(pointer)
    if guard is not None:
        guard()
    uuid = getattr(board, "m_Uuid", None)
    if guard is not None:
        guard()
    uuid_text = uuid.AsString() if callable(getattr(uuid, "AsString", None)) else ""
    if guard is not None:
        guard()
    filename = str(board.GetFileName())
    if guard is not None:
        guard()
    return address, str(uuid_text), filename


class ImpedanceStorageError(RuntimeError):
    """Saved impedance intent cannot currently be read safely."""


class _CaptureSuperseded(ValidationError):
    """A newer capture operation owns the session and its native resources."""


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
            invalidate_source = getattr(
                self.controls, "_invalidate_output_source", None
            )
            if callable(invalidate_source):
                invalidate_source()
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
            published_guard: Optional[Callable[[], None]] = None
            try:
                current = self.controls.snapshot_output_board(owner.board)
                reader = getattr(self.controls, "_output_source_reader", None)
                if isinstance(reader, SourceSnapshotReader):
                    published_guard = reader.lifetime_guard()
                if self.controls._source_revision() != revision:
                    raise ValidationError(
                        "The board or output variant changed while preparing review. "
                        "Review the current output before approval."
                    )
                self.owner, self.revision, self.current = owner, revision, current
            except BaseException:
                try:
                    if published_guard is not None:
                        with suppress(SourceSnapshotChanged):
                            published_guard()
                            self.controls._invalidate_output_source()
                finally:
                    owner.close()
                raise
        assert self.current is not None
        return self.current


class CaptureSession:
    """Own one board/appearance revision and its bounded native capture cache."""

    def __init__(self, controls: "ImpedanceControls", directory: Path) -> None:
        self.controls = controls
        self.directory = directory
        self.revision: Optional[CaptureRevision] = None
        self.renderer: Any = None
        self._capture_epoch = 0
        self.images: OrderedDict[Section, CapturedImage] = OrderedDict()
        self.image_bytes = 0
        self.spacing_images: OrderedDict[SpacingReviewEntry, CapturedImage] = (
            OrderedDict()
        )
        self.spacing_image_bytes = 0
        self._evidence_revision: Optional[CaptureRevision] = None
        self._preparation_revision: Optional[CaptureRevision] = None
        self._hashes: dict[Section, Optional[str]] = {}
        self._spacing_hashes: OrderedDict[SpacingReviewEntry, Optional[str]] = (
            OrderedDict()
        )
        self._spacing_policy: Optional[tuple[int, int]] = None
        if not isinstance(
            getattr(controls, "_owned_capture_cache", None), OwnedCaptureCache
        ):
            controls._owned_capture_cache = OwnedCaptureCache()
        self.owned_captures = controls._owned_capture_cache
        self._pending_owned: OrderedDict[CaptureKey, CapturedImage] = OrderedDict()
        self._pending_owned_bytes = 0
        self.output = (
            OutputBoardSession(controls, self._close_captures)
            if controls._selected_output_variant() is not None
            else None
        )

    def _close_renderer(self) -> None:
        """Release plots and PNG bytes independently of verified image evidence."""
        self._capture_epoch += 1
        renderer, self.renderer = self.renderer, None
        self._preparation_revision = None
        self.images.clear()
        self.image_bytes = 0
        self.spacing_images.clear()
        self.spacing_image_bytes = 0
        self._pending_owned.clear()
        self._pending_owned_bytes = 0
        if renderer is not None:
            renderer.close()

    def _close_captures(self) -> None:
        """Release renderers before replacing a detached output source."""
        self.revision = None
        self._preparation_revision = None
        self._hashes.clear()
        self._spacing_hashes.clear()
        self._spacing_policy = None
        # Retain the old revision marker, without evidence, so Save can distinguish
        # invalidated review from a new session's first historical restoration.
        self._close_renderer()

    def close(self) -> None:
        """Release native resources and all captures together, including on failure."""
        try:
            if self.output is None:
                self._close_captures()
            else:
                self.output.close()
        finally:
            self._evidence_revision = None

    def snapshot(self) -> BoardSnapshot:
        """Check the live board identity and invalidate an obsolete capture revision."""
        try:
            variant = self.controls._selected_output_variant()
            if (self.output is None) != (variant is None):
                if self.output is None:
                    self._close_captures()
                else:
                    self.output.close()
                self.output = (
                    OutputBoardSession(self.controls, self._close_captures)
                    if variant is not None
                    else None
                )
            current = (
                self.controls._snapshot()
                if self.output is None
                else self.output.snapshot()
            )
            if self.revision is not None and current != self.revision[0]:
                self._close_captures()
            if self._evidence_revision is None:
                revision = self._verification_revision(current)
                if revision is not None:
                    self._adopt_revision(revision)
            return current
        except Exception:
            self.close()
            raise

    def _capture_revision(
        self, snapshot: BoardSnapshot, appearance: object
    ) -> CaptureRevision:
        """Include independent output selection and rendering policy provenance."""
        from .matching import CAPTURE_POLICY_REVISION

        return (
            snapshot,
            appearance,
            self.controls._selected_output_variant(),
            CAPTURE_POLICY_REVISION,
        )

    def _verification_revision(
        self, snapshot: BoardSnapshot
    ) -> Optional[CaptureRevision]:
        """Block approval for unreadable appearance while allowing draft persistence."""
        try:
            appearance = read_theme_context(self.controls.parent.pcbnew)
        except Exception as error:
            self._close_captures()
            _LOG.info("Impedance capture appearance unavailable: %s", error)
            return None
        return self._capture_revision(snapshot, appearance)

    def _adopt_revision(self, revision: CaptureRevision) -> None:
        """Start capture work only after its source and appearance were checked."""
        if revision != self.revision:
            self._close_captures()
            self.revision = revision
        self._evidence_revision = revision

    def _ensure_renderer(self) -> None:
        """Share native layer plots while keeping diagnostic image evidence separate."""
        from .render import SectionRenderer

        if self.renderer is None:
            expected = self.revision
            current = self.snapshot()
            if expected is None or self._verification_revision(current) != expected:
                self._close_captures()
                raise ValidationError(
                    "The capture source or appearance changed before rendering."
                )
            epoch = self._capture_epoch
            output = self.output
            owner = output.owner if output is not None else None
            readers = [getattr(self.controls, "_live_source_reader", None)]
            if output is not None:
                readers.append(getattr(self.controls, "_output_source_reader", None))
            guards = tuple(
                reader.lifetime_guard()
                for reader in readers
                if isinstance(reader, SourceSnapshotReader)
            )

            def check_owner() -> None:
                """Reject native reentry before publishing an unpublished renderer."""
                if (
                    self._capture_epoch != epoch
                    or self.revision is not expected
                    or self.renderer is not None
                    or self.output is not output
                    or getattr(self.controls, "_closed", False)
                    or self.controls._selected_output_variant() != expected[2]
                    or (
                        output is not None
                        and (
                            output.owner is not owner or getattr(owner, "closed", False)
                        )
                    )
                ):
                    raise _CaptureSuperseded(
                        "The capture session changed or closed while creating its renderer."
                    )
                try:
                    for guard in guards:
                        guard()
                except SourceSnapshotChanged as error:
                    raise _CaptureSuperseded(str(error)) from error

            pcbnew = self.controls.parent.pcbnew
            guarded_live = getattr(self.controls, "_guard_live_board", None)
            candidate = None
            try:
                live = guarded_live(check_owner) if callable(guarded_live) else None
                identity = (
                    _source_board_key(live, check_owner) if live is not None else None
                )
                board = (
                    (live if live is not None else pcbnew.GetBoard())
                    if output is None
                    else output.board
                )
                check_owner()
                candidate = SectionRenderer(
                    board,
                    pcbnew,
                    appearance=self.revision[1] if self.revision else None,
                )
                check_owner()
                if (
                    callable(guarded_live)
                    and _source_board_key(guarded_live(check_owner), check_owner)
                    != identity
                ):
                    raise _CaptureSuperseded(
                        "The source board changed while creating its renderer."
                    )
            except BaseException as error:
                failure = error
                if isinstance(error, Exception):
                    try:
                        check_owner()
                    except BaseException as guard_error:
                        failure = guard_error
                if candidate is not None:
                    try:
                        candidate.close()
                    except BaseException as cleanup_error:
                        if not isinstance(error, Exception):
                            raise error from cleanup_error
                        if not isinstance(cleanup_error, Exception):
                            raise
                        raise failure from cleanup_error
                if failure is error:
                    raise
                raise failure from error
            self.renderer = candidate

    def _owned_key(
        self, item: Union[Section, "SpacingReviewEntry"]
    ) -> Optional[CaptureKey]:
        """Identify pure pixels independently of this session's native resources."""
        if self.revision is None:
            raise ValidationError("The capture source has not been verified.")
        snapshot, appearance, variant, _policy = self.revision
        try:
            identity = getattr(self.controls, "board_path", None)
            if identity is None:
                board = self.controls.parent.pcbnew.GetBoard()
                filename = getattr(board, "GetFileName", None)
                identity = filename() if callable(filename) else None
            if not isinstance(identity, str) or not identity.strip():
                return None
            board_identity = str(Path(identity).resolve())
        except (RuntimeError, OSError, ValueError):
            # Pixel reuse is optional. An unavailable stable filename must not
            # turn a previously supported native capture into a new failure.
            return None
        return capture_key(
            self.owned_captures,
            board_identity,
            snapshot,
            appearance,
            variant,
            item,
            dimensions=(item.width_px, item.height_px)
            if hasattr(item, "view_id")
            else (IMAGE_WIDTH, IMAGE_HEIGHT),
        )

    def _commit_owned(self) -> None:
        """Publish staged pixels only after a fresh source/appearance boundary."""
        for key, image in self._pending_owned.items():
            self.owned_captures.put(key, image)
        self._pending_owned.clear()
        self._pending_owned_bytes = 0

    def _discard_pending(self, key: Optional[CaptureKey]) -> None:
        """Remove staged pixels before a forced refresh, including failed refresh."""
        if key is None:
            return
        previous = self._pending_owned.pop(key, None)
        if previous is not None:
            self._pending_owned_bytes -= previous.byte_size

    def _stage_owned(self, key: Optional[CaptureKey], capture: CapturedImage) -> None:
        """Keep speculative ownership bounded until the next provenance check."""
        if key is None:
            return
        self._discard_pending(key)
        if capture.byte_size > MAX_OWNED_CAPTURE_BYTES:
            return
        while self._pending_owned and (
            self._pending_owned_bytes + capture.byte_size > MAX_OWNED_CAPTURE_BYTES
        ):
            self._discard_pending(next(iter(self._pending_owned)))
        self._pending_owned[key] = capture
        self._pending_owned_bytes += capture.byte_size

    def _render(self, section: Section) -> CapturedImage:
        """Render within a caller-checked revision without per-image source reads."""
        key = self._owned_key(section)
        capture = self.owned_captures.get(key) if key is not None else None
        if capture is None:
            self._ensure_renderer()
            path = self.renderer.render(
                section, self.directory / f"{section.section_id}.png"
            )
            capture = load_rendered_capture(
                path,
                self.renderer,
                section,
                width_px=IMAGE_WIDTH,
                height_px=IMAGE_HEIGHT,
            )
            self._stage_owned(key, capture)
        while (
            self.images
            and self.image_bytes + len(capture.data) > MAX_CAPTURE_CACHE_BYTES
        ):
            _, evicted = self.images.popitem(last=False)
            self.image_bytes -= len(evicted.data)
        self.images[section] = capture
        self.image_bytes += len(capture.data)
        return capture

    def _render_spacing(self, entry: "SpacingReviewEntry") -> CapturedImage:
        """Rasterize a diagnostic viewport without altering manufacturing captures."""
        key = self._owned_key(entry)
        capture = self.owned_captures.get(key) if key is not None else None
        if capture is None:
            self._ensure_renderer()
            identity = hashlib.sha256(entry.view_id.encode("utf-8")).hexdigest()
            destination = self.directory / f"spacing-{identity}.png"
            result = Path(
                self.renderer.render_spacing(
                    entry,
                    destination,
                    width_px=entry.width_px,
                    height_px=entry.height_px,
                )
            )
            if result.resolve() != destination.resolve():
                raise ValidationError(
                    "The spacing renderer did not create its expected PNG."
                )
            capture = load_spacing_capture(
                result, self.renderer, entry, include_original=False
            )
            self._stage_owned(key, capture)
        if (capture.width, capture.height) != (entry.width_px, entry.height_px):
            raise ValidationError("The spacing capture has unexpected dimensions.")
        while (
            self.spacing_images
            and self.spacing_image_bytes + capture.byte_size
            > MAX_SPACING_CAPTURE_CACHE_BYTES
        ):
            _, evicted = self.spacing_images.popitem(last=False)
            self.spacing_image_bytes -= evicted.byte_size
        self.spacing_images[entry] = capture
        self.spacing_image_bytes += capture.byte_size
        return capture

    def _current_spacing_policy(self) -> tuple[int, int]:
        """Read diagnostic policies independently of workbook capture identity."""
        from .spacing_checks import SPACING_CHECK_POLICY_REVISION
        from .spacing_review import SPACING_RENDER_POLICY_REVISION

        return SPACING_CHECK_POLICY_REVISION, SPACING_RENDER_POLICY_REVISION

    def _clear_spacing_captures(self) -> None:
        """Discard diagnostic evidence without invalidating manufacturing proof."""
        self.spacing_images.clear()
        self.spacing_image_bytes = 0
        self._spacing_hashes.clear()

    def _adopt_spacing_policy(self) -> tuple[int, int]:
        """Explicit capture work may establish current diagnostic policy evidence."""
        policy = self._current_spacing_policy()
        if self._spacing_policy != policy:
            self._clear_spacing_captures()
            self._spacing_policy = policy
        return policy

    def _require_spacing_policy(self, expected: tuple[int, int]) -> None:
        """Never retain captures whose diagnostic policy changed during rendering."""
        if self._current_spacing_policy() != expected:
            self._clear_spacing_captures()
            raise ValidationError("The spacing capture policy changed during review.")

    def _put_spacing_hash(
        self, entry: "SpacingReviewEntry", digest: Optional[str]
    ) -> None:
        """Bound actual diagnostic evidence to one complete current review set."""
        from .spacing_review import MAX_SPACING_REVIEW_IMAGES

        self._spacing_hashes.pop(entry, None)
        self._spacing_hashes[entry] = digest
        while len(self._spacing_hashes) > MAX_SPACING_REVIEW_IMAGES:
            self._spacing_hashes.popitem(last=False)

    def prepare(
        self,
        expected_snapshot: BoardSnapshot,
        section: Union[Section, "SpacingReviewEntry"],
    ) -> CapturedImage:
        """Warm owned geometry; live validation belongs to presentation and Save.

        The first render binds the existing detached board. Subsequent idle turns
        use that same geometry, never treating speculative work as a view event.
        """
        if hasattr(section, "view_id"):
            return self.prepare_spacing(expected_snapshot, section)
        revision = self._verification_revision(expected_snapshot)
        if revision is None:
            raise ValidationError("The capture appearance is unavailable.")
        if self._evidence_revision is not None and revision != self._evidence_revision:
            self._close_captures()
            raise ValidationError("The capture appearance or output variant changed.")
        if self.renderer is None and self._preparation_revision != revision:
            if self.snapshot() != expected_snapshot:
                raise ValidationError("The board changed before capture preparation.")
            self._adopt_revision(revision)
            self._preparation_revision = revision
        elif revision != self.revision:
            self._close_captures()
            raise ValidationError("The capture appearance or output variant changed.")
        cached = self.images.get(section)
        if cached is not None:
            self.images.move_to_end(section)
            return cached
        self._hashes[section] = None
        try:
            capture = self._render(section)
        except _CaptureSuperseded:
            raise
        except Exception:
            self._close_renderer()
            raise
        self._hashes[section] = capture.sha256
        return capture

    def prepare_spacing(
        self, expected_snapshot: BoardSnapshot, entry: "SpacingReviewEntry"
    ) -> CapturedImage:
        """Warm diagnostic pixels without recording a view or granting consent."""
        policy = self._adopt_spacing_policy()
        revision = self._verification_revision(expected_snapshot)
        if revision is None:
            raise ValidationError("The capture appearance is unavailable.")
        if self._evidence_revision is not None and revision != self._evidence_revision:
            self._close_captures()
            raise ValidationError("The capture appearance or output variant changed.")
        if self.renderer is None and self._preparation_revision != revision:
            if self.snapshot() != expected_snapshot:
                raise ValidationError("The board changed before capture preparation.")
            self._adopt_revision(revision)
            self._preparation_revision = revision
        elif revision != self.revision:
            self._close_captures()
            raise ValidationError("The capture appearance or output variant changed.")
        self._spacing_policy = policy
        cached = self.spacing_images.get(entry)
        if cached is not None:
            self.spacing_images.move_to_end(entry)
            self._require_spacing_policy(policy)
            self._put_spacing_hash(entry, cached.sha256)
            return cached
        self._put_spacing_hash(entry, None)
        try:
            capture = self._render_spacing(entry)
            self._require_spacing_policy(policy)
        except _CaptureSuperseded:
            raise
        except Exception:
            self._close_renderer()
            raise
        self._put_spacing_hash(entry, capture.sha256)
        return capture

    def verify_spacing_captures(
        self,
        expected_snapshot: BoardSnapshot,
        entries: tuple["SpacingReviewEntry", ...],
    ) -> SpacingCaptureEvidence:
        """Verify diagnostic evidence with fresh source checks at both boundaries.

        Owned immutable PNG hashes survive byte-cache eviction within the same
        checked native source/appearance revision. A failed entry stays None
        within the current required set until a successful preparation or
        preview replaces it. Entries outside that set or the bounded ledger can
        be forgotten; requesting them again requires current owned PNG proof.
        This ledger cannot approve images or alter manufacturing evidence.
        """
        started = perf_counter()
        failed = SpacingCaptureEvidence(False, dict.fromkeys(entries))
        try:
            from .spacing_review import MAX_SPACING_REVIEW_IMAGES

            if len(set(entries)) > MAX_SPACING_REVIEW_IMAGES:
                self._clear_spacing_captures()
                return failed
            policy = self._current_spacing_policy()
            was_unchecked = self._evidence_revision is None
            current = self.snapshot()
            # snapshot() checks the initial appearance while establishing its
            # first revision; later calls need an explicit fresh appearance read.
            revision = (
                self._evidence_revision
                if was_unchecked
                else self._verification_revision(current)
            )
            if (
                current != expected_snapshot
                or revision is None
                or (
                    self._evidence_revision is not None
                    and revision != self._evidence_revision
                )
            ):
                self._close_captures()
                return failed
            if self._spacing_policy is not None and policy != self._spacing_policy:
                self._clear_spacing_captures()
                return failed
            self._adopt_revision(revision)
            self._spacing_policy = policy
            required = set(entries)
            for obsolete in tuple(self._spacing_hashes):
                if obsolete not in required:
                    self._spacing_hashes.pop(obsolete)
            missing = tuple(
                dict.fromkeys(
                    entry for entry in entries if entry not in self._spacing_hashes
                )
            )
            for entry in missing:
                self._put_spacing_hash(entry, None)
                try:
                    capture = self.spacing_images.get(entry)
                    if capture is None:
                        capture = self._render_spacing(entry)
                    self._put_spacing_hash(entry, capture.sha256)
                except _CaptureSuperseded:
                    return failed
                except Exception as error:
                    self._close_renderer()
                    _LOG.info("Spacing capture verification failed: %s", error)
            if self._verification_revision(self.snapshot()) != revision:
                self._close_captures()
                return failed
            if self._current_spacing_policy() != policy:
                self._clear_spacing_captures()
                return failed
            self._commit_owned()
            _LOG.info(
                "Spacing capture evidence: images=%d retained=%d rendered=%d elapsed=%.3fs",
                len(entries),
                len(entries) - len(missing),
                len(missing),
                perf_counter() - started,
            )
            return SpacingCaptureEvidence(
                True, {entry: self._spacing_hashes[entry] for entry in entries}
            )
        except Exception as error:
            self._close_captures()
            _LOG.info("Spacing capture revision verification failed: %s", error)
            return failed

    def verify_captures(
        self, expected_snapshot: BoardSnapshot, sections: tuple[Section, ...]
    ) -> CaptureEvidence:
        """Reuse session evidence, checking only batch boundaries for missing images.

        The caller supplies a freshly read snapshot. Historical hashes alone never
        enter this ledger: missing evidence is rendered once, and None records a
        failure until a successful explicit preview or refresh replaces it.
        """
        started = perf_counter()
        revision = self._verification_revision(expected_snapshot)
        if revision is None or (
            self._evidence_revision is not None and revision != self._evidence_revision
        ):
            self._close_captures()
            return CaptureEvidence(False, dict.fromkeys(sections))
        self._adopt_revision(revision)
        missing = tuple(
            dict.fromkeys(row for row in sections if row not in self._hashes)
        )
        for section in missing:
            self._hashes[section] = None
            try:
                self._hashes[section] = self._render(section).sha256
            except _CaptureSuperseded:
                return CaptureEvidence(False, dict.fromkeys(sections))
            except Exception as error:
                self._close_renderer()
                _LOG.info("Impedance capture verification failed: %s", error)
        if missing and self._verification_revision(self.snapshot()) != revision:
            self._close_captures()
            return CaptureEvidence(False, dict.fromkeys(sections))
        self._commit_owned()
        _LOG.info(
            "Impedance capture evidence: images=%d retained=%d rendered=%d elapsed=%.3fs",
            len(sections),
            len(sections) - len(missing),
            len(missing),
            perf_counter() - started,
        )
        return CaptureEvidence(True, {row: self._hashes[row] for row in sections})

    def verify(self, expected: BoardSnapshot) -> None:
        """Reject a changed source without adopting it in a nested editor."""
        current = self.snapshot()
        revision = self._verification_revision(current)
        if current != expected or revision is None:
            raise ValidationError(
                "The board changed. Review the updated workbook rows before approval."
            )
        if revision == self.revision:
            self._commit_owned()

    def preview(
        self,
        section: Union[Section, "SpacingReviewEntry"],
        *,
        refresh: bool = False,
    ) -> CapturedImage:
        """Check before capture; dialogs verify returned pixels before and after paint."""
        if hasattr(section, "view_id"):
            return self.preview_spacing(section, refresh=refresh)
        self._capture_epoch += 1
        revision = None
        try:
            revision = self._capture_revision(
                self.snapshot(), read_theme_context(self.controls.parent.pcbnew)
            )
            self._adopt_revision(revision)
            if refresh:
                self.owned_captures.discard(self._owned_key(section))
                self._discard_pending(self._owned_key(section))
                previous = self.images.pop(section, None)
                if previous is not None:
                    self.image_bytes -= len(previous.data)
            cached = self.images.get(section)
            if cached is not None:
                self.images.move_to_end(section)
                return cached
            self._hashes[section] = None
            capture = self._render(section)
            self._hashes[section] = capture.sha256
            return capture
        except _CaptureSuperseded:
            raise
        except Exception:
            try:
                self._close_captures()
            finally:
                if revision is not None:
                    self.revision = self._evidence_revision = revision
                    self._hashes[section] = None
            raise

    def preview_spacing(
        self, entry: "SpacingReviewEntry", *, refresh: bool = False
    ) -> CapturedImage:
        """Check live provenance and retain diagnostic PNGs in their own cache."""
        self._capture_epoch += 1
        revision = None
        try:
            policy = self._adopt_spacing_policy()
            revision = self._capture_revision(
                self.snapshot(), read_theme_context(self.controls.parent.pcbnew)
            )
            self._adopt_revision(revision)
            self._spacing_policy = policy
            if refresh:
                self.owned_captures.discard(self._owned_key(entry))
                self._discard_pending(self._owned_key(entry))
                previous = self.spacing_images.pop(entry, None)
                if previous is not None:
                    self.spacing_image_bytes -= previous.byte_size
            cached = self.spacing_images.get(entry)
            if cached is not None:
                self.spacing_images.move_to_end(entry)
                self._require_spacing_policy(policy)
                self._put_spacing_hash(entry, cached.sha256)
                return cached
            self._put_spacing_hash(entry, None)
            capture = self._render_spacing(entry)
            self._require_spacing_policy(policy)
            self._put_spacing_hash(entry, capture.sha256)
            return capture
        except _CaptureSuperseded:
            raise
        except Exception:
            try:
                if self._current_spacing_policy() != policy:
                    self._clear_spacing_captures()
                else:
                    self._close_captures()
            finally:
                if revision is not None:
                    self.revision = self._evidence_revision = revision
                    self._put_spacing_hash(entry, None)
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
        self._closed = False
        self._live_source_reader = SourceSnapshotReader()
        self._output_source_reader = SourceSnapshotReader()
        self._owned_capture_cache = OwnedCaptureCache()
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
        if getattr(self, "_closed", False):
            raise ValidationError("The controlled-impedance window is closed.")
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
                # Validation is also used by optional caches. Only an
                # authoritative settings load owns storage-error UI state.
                raise ImpedanceStorageError(str(exc)) from exc

    def reload(self) -> Config:
        """Read a fresh database revision, retaining errors for export preflight."""
        try:
            self.check_board()
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
            label = (
                f"{specs} specs · {count} sections with saved approval - All reviewed"
            )
        elif self.config.enabled or specs or self.config.stackup is not None:
            label = f"{specs} specs - Needs review"
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
        """Acquire fresh source proof before reusing an owned immutable snapshot."""
        from . import pcbnew_adapter, source_evidence

        if self._closed:
            raise ValidationError("The controlled-impedance window is closed.")
        initial_guard = self._live_source_reader.lifetime_guard()
        try:
            board = self._guard_live_board(initial_guard)
            identity = _source_board_key(board, initial_guard)
        except BaseException:
            initial_guard()
            self._live_source_reader.invalidate()
            raise

        def current(guard: Callable[[], None]) -> Any:
            """Recheck native lifetime before each evidence read or construction."""
            live = self._guard_live_board(guard)
            if _source_board_key(live, guard) != identity:
                raise ValidationError("The source board changed during reading.")
            return live

        def acquire() -> source_evidence.SourceEvidence:
            """Reject replacement or close dispatched during a native proof read."""
            guard = self._live_source_reader.lifetime_guard()
            evidence = source_evidence.acquire_source_evidence(
                current(guard), self.parent.pcbnew
            )
            guard()
            current(guard)
            return evidence

        def build() -> BoardSnapshot:
            """Keep native ownership valid through the synchronous build boundary."""
            guard = self._live_source_reader.lifetime_guard()
            return pcbnew_adapter.snapshot_board(current(guard), self.parent.pcbnew)

        return self._live_source_reader.read(
            acquire,
            build,
            owner_key=(
                "live",
                _SOURCE_READER_POLICY,
                identity,
                id(pcbnew_adapter.snapshot_board),
            ),
        )

    def _guard_live_board(self, guard: Optional[Callable[[], None]] = None) -> Any:
        """Apply the main window's board-lifetime guard for ordinary boards too."""
        if guard is not None:
            guard()
        self.check_board()
        if guard is not None:
            guard()
        get_board = getattr(self.parent, "_legacy_board", None)
        board = get_board() if callable(get_board) else self.parent.pcbnew.GetBoard()
        if guard is not None:
            guard()
        return board

    def _invalidate_output_source(self) -> None:
        """Discard detached source state before its native allocation is released."""
        self._output_source_reader.invalidate()

    def close(self) -> None:
        """Reject queued work and release immutable state after accepted close."""
        self._closed = True
        self._live_source_reader.close()
        self._output_source_reader.close()

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
        from . import pcbnew_adapter, source_evidence

        if self._closed:
            raise ValidationError("The controlled-impedance window is closed.")
        initial_guard = self._output_source_reader.lifetime_guard()
        try:
            live = self._guard_live_board(initial_guard)
            name = self._selected_output_variant()
            initial_guard()
            identity = _source_board_key(board, initial_guard)
            metadata_identity = _source_board_key(live, initial_guard)
        except BaseException:
            initial_guard()
            self._output_source_reader.invalidate()
            raise

        def current_metadata(guard: Callable[[], None]) -> Any:
            """Keep selection and live metadata ownership fresh on every read."""
            current = self._guard_live_board(guard)
            selected = self._selected_output_variant()
            guard()
            current_variant = (
                str(board.GetCurrentVariant()) if name is not None else None
            )
            guard()
            if selected != name or current_variant != name:
                raise ValidationError(
                    "The fabrication output variant changed. Review its current output."
                )
            if (
                _source_board_key(board, guard) != identity
                or _source_board_key(current, guard) != metadata_identity
            ):
                raise ValidationError("The output source board changed during reading.")
            return current if name is not None else None

        def acquire() -> source_evidence.SourceEvidence:
            """Read output geometry and, for variants, the live class definitions."""
            guard = self._output_source_reader.lifetime_guard()
            metadata = current_metadata(guard)
            evidence = source_evidence.acquire_source_evidence(
                board, self.parent.pcbnew, netclass_source=metadata
            )
            guard()
            current_metadata(guard)
            return evidence

        def build() -> BoardSnapshot:
            """Publish the final variant-bound object, avoiding repeated replace()."""
            guard = self._output_source_reader.lifetime_guard()
            metadata = current_metadata(guard)
            snapshot = pcbnew_adapter.snapshot_board(
                board, self.parent.pcbnew, netclass_source=metadata
            )
            if name is None:
                return snapshot
            context = json.dumps((snapshot.context_digest, name), ensure_ascii=True)
            return replace(
                snapshot,
                context_digest=hashlib.sha256(context.encode("ascii")).hexdigest(),
            )

        return self._output_source_reader.read(
            acquire,
            build,
            owner_key=(
                "output",
                _SOURCE_READER_POLICY,
                identity,
                metadata_identity,
                name,
                id(pcbnew_adapter.snapshot_board),
            ),
        )

    def verify_output_board(
        self, plan: ExportPlan, board: Any, layer_count: Optional[int]
    ) -> None:
        """Reject a report/plot board that differs from the approved output context."""
        validate_current(
            plan, plan.config, self.snapshot_output_board(board), layer_count
        )

    def verify_report_captures(
        self, plan: ExportPlan, reports: ExportArtifacts, board: Any
    ) -> None:
        """Recheck rendered output and appearance without matching or rendering again."""
        if self.snapshot_output_board(board) != plan.snapshot:
            raise ValidationError(
                "The report output changed after its captures were rendered. "
                "Review the current output and generate again."
            )
        if reports.appearance is not None and (
            read_theme_context(self.parent.pcbnew) != reports.appearance
        ):
            raise ValidationError(
                "The appearance changed after the reviewed captures were rendered. "
                "Open Configure to review the current images, then generate again."
            )

    def export_capture_options(self, plan: ExportPlan, board: Any) -> dict[str, Any]:
        """Offer owned pixels only with fresh output and appearance verification."""
        self.check_board()
        if self.snapshot_output_board(board) != plan.snapshot:
            raise ValidationError("The report output changed before capture reuse.")
        return {
            "capture_cache": self._owned_capture_cache,
            "capture_identity": (
                str(Path(self.board_path).resolve()),
                self._selected_output_variant(),
            ),
            "appearance_context": lambda: read_theme_context(self.parent.pcbnew),
            "source_context": lambda: self.snapshot_output_board(board),
        }

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

                def load_calculator_config() -> CalculatorConfigCache:
                    """Read reusable calculator metadata without altering board intent."""
                    self.check_board()
                    if self.database is None:
                        raise ValidationError("The project database is not ready.")
                    return ImpedanceRepository.read_calculator_config(self.database)

                def save_calculator_config(cache: CalculatorConfigCache) -> None:
                    """Persist a successful calculator-config fetch."""
                    self._writable_repository().save_calculator_config(cache)

                def load_width_results() -> tuple[WidthResult, ...]:
                    """Read this board's cached solves; an unregistered PCB has none."""
                    self.check_board()
                    if self.repository is None:
                        return ()
                    return self.repository.load_width_results()

                dialog_active = True
                dialog_owner: Optional[ReferenceType[Any]] = None

                def save_width_result(result: WidthResult) -> None:
                    """Persist one settled solve as it arrives, without a config save.

                    Workers call this per job. Board identity (a pcbnew call) and
                    registration must stay on the GUI thread, so the write is
                    marshalled there; it still lands before the batch completes.
                    """

                    def write() -> None:
                        owner = dialog_owner() if dialog_owner is not None else None
                        if (
                            not dialog_active
                            or owner is None
                            or getattr(owner, "_closing", False)
                        ):
                            return
                        try:
                            self._writable_repository().save_width_result(result)
                        except Exception as exc:
                            _LOG.info("JLCPCB width: cache write skipped (%s)", exc)

                    wx.CallAfter(write)

                dialog = ImpedanceDialog(
                    self.parent,
                    config,
                    snapshot,
                    captures.preview,
                    captures.snapshot,
                    verify_snapshot=captures.verify,
                    verify_captures=captures.verify_captures,
                    verify_spacing_captures=captures.verify_spacing_captures,
                    prepare_capture=captures.prepare,
                    appearance_context=lambda: (
                        read_theme_context(self.parent.pcbnew),
                        self._selected_output_variant(),
                    ),
                    load_stackup_catalog=load_stackup_catalog,
                    save_stackup_catalog=save_stackup_catalog,
                    load_calculator_config=load_calculator_config,
                    save_calculator_config=save_calculator_config,
                    load_stackup_colors=lambda: read_stackup_copper_colors(
                        self.parent.pcbnew
                    ),
                    save_config=self._save,
                    load_width_results=load_width_results,
                    save_width_result=save_width_result,
                    skip_stub_review=lambda: bool(
                        self.parent.settings.get("impedance", {}).get(
                            "skip_stub_review", True
                        )
                    ),
                )
                dialog_owner = ref(dialog)
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
                    dialog_active = False
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
