"""Handles the generation of the Gerber files, the BOM and the POS file."""

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
import csv
from dataclasses import dataclass
import hashlib
from importlib import import_module
import logging
import math
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Optional
import unicodedata
import weakref

from pcbnew import (  # pylint: disable=import-error
    DRILL_MARKS_NO_DRILL_SHAPE,
    EXCELLON_WRITER,
    PCB_VIA,
    PLOT_CONTROLLER,
    PLOT_FORMAT_GERBER,
    ZONE_FILLER,
    B_Cu,
    B_Mask,
    B_Paste,
    B_SilkS,
    Edge_Cuts,
    F_Cu,
    F_Mask,
    F_Paste,
    F_SilkS,
    FromMM,
    IsCopperLayer,
    Refresh,
    ToMM,
    wxPoint,
)

from .correction_data import (
    AnyCorrection,
    Correction,
    CorrectionMatch,
    LcscCorrection,
    match_correction,
    resolve_shared_corrections,
)
from .fabrication_archive import (
    ArchiveEntry,
    artifact_publication,
    build_archive,
    collect_gerber_entries,
)
from .footprint_helpers import get_is_dnp

# JLC rejects BOM rows whose total length exceeds 2048 characters.  We budget
# 128 characters of headroom for the other fields (Comment, Footprint, LCSC,
# Quantity) so the Designator chunk alone is capped at 1920 characters.
_BOM_DESIGNATOR_MAX_LEN = 1920  # 2048 - 128 padding for remaining CSV fields


@dataclass(frozen=True)
class OutputAssemblySnapshot:
    """Keep BOM and CPL on one native assembly state throughout generation."""

    bom_rows: tuple[tuple[Any, ...], ...]
    cpl_rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class _OrdinaryGeneration:
    """Retain ordinary assembly rows together with their native source check."""

    output: OutputAssemblySnapshot
    validate_source: Callable[[], None]


@dataclass
class _Generation:
    """Own the captured source, isolated plot board, and staging directory together."""

    directory: TemporaryDirectory
    output: OutputAssemblySnapshot
    validate_source: Callable[[], None]
    geometry: tuple[Any, ...]
    original_gerberdir: str
    plot_board: Any = None
    plot_source_digest: Optional[str] = None  # noqa: UP045
    plot_project_properties: tuple[tuple[str, str], ...] = ()


def _variant_filename_label(name: str) -> str:
    """Keep variant names readable while removing unsafe filename characters."""
    return re.sub(
        r'[<>:"/\\|?*\x00-\x1f]+', "-", unicodedata.normalize("NFC", name)
    ).strip(" .-")


def _checked_position(x: float, y: float) -> Any:
    """Reject overflow before wxPoint converts doubles to signed 32-bit integers."""
    if any(not -(2**31) <= int(value) <= 2**31 - 1 for value in (x, y)):
        raise ValueError("position exceeds KiCad's signed 32-bit coordinate range")
    return wxPoint(x, y)


def split_bom_designators(
    designators: list, max_len: int = _BOM_DESIGNATOR_MAX_LEN
) -> list:
    """Split a list of reference designators into chunks whose joined length fits within *max_len*.

    JLCPCB rejects BOM rows whose total row length exceeds 2048 characters.
    The Designator field is capped below that limit to leave headroom for the
    other fields (Comment, Footprint, LCSC, Quantity).  When a single part has
    more references than fit in the limit, the row is duplicated with the
    designators spread across copies; each copy carries only its own count.

    Args:
        designators: Ordered list of reference strings, e.g. ``["R1", "R2", ...]``.
        max_len: Maximum allowed byte-length of the comma-joined designator string.

    Returns:
        A list of non-empty lists, each safe to pass to ``",".join()``.

    """
    if not designators:
        return []
    chunks = []
    current: list = []
    current_len = 0
    for ref in designators:
        # Length if this ref were appended: len(ref) plus the comma separator
        added = len(ref) if not current else len(ref) + 1
        if current and current_len + added > max_len:
            chunks.append(current)
            current = [ref]
            current_len = len(ref)
        else:
            current.append(ref)
            current_len += added
    if current:
        chunks.append(current)
    return chunks


class Fabrication:
    """Contains all functionality to generate the JLCPCB production files."""

    def __init__(self, parent: Any, board: Any) -> None:
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.board = board
        self.corrections: tuple[AnyCorrection, ...] = ()
        self.variant_name = ""
        self._generation: Optional[_Generation] = None  # noqa: UP045
        self._ordinary_generation: Optional[_OrdinaryGeneration] = None  # noqa: UP045
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.create_folders()

    def create_folders(self) -> None:
        """Create output folders if they not already exist."""
        self.outputdir = os.path.join(self.path, "jlcpcb", "production_files")
        Path(self.outputdir).mkdir(parents=True, exist_ok=True)
        self.gerberdir = os.path.join(
            self.path, "jlcpcb", "gerber", Path(self.filename).stem
        )
        Path(self.gerberdir).mkdir(parents=True, exist_ok=True)

    @property
    def output_snapshot(self) -> Optional[OutputAssemblySnapshot]:  # noqa: UP045
        """Expose only the frozen rows of the current generation."""
        operation = getattr(self, "_generation", None)
        if operation is None:
            operation = getattr(self, "_ordinary_generation", None)
        return operation.output if operation is not None else None

    def _validate_ordinary_board(self) -> None:
        """Ask the owning window to reject replaced or unavailable editor boards."""
        get_board = getattr(self.parent, "_get_current_board", None)
        if callable(get_board):
            get_board()

    @staticmethod
    def _ordinary_mapping(
        parts: Iterable[dict[str, Any]],
    ) -> tuple[tuple[Any, ...], ...]:
        """Compare assembly decisions without supplier cache or table sort state."""
        keys = (
            "reference",
            "value",
            "footprint",
            "lcsc",
            "exclude_from_bom",
            "exclude_from_pos",
            "is_dnp",
        )
        return tuple(sorted(tuple(part[key] for key in keys) for part in parts))

    def begin_ordinary_generation(
        self, corrections: tuple[AnyCorrection, ...]
    ) -> OutputAssemblySnapshot:
        """Freeze ordinary BOM, placements and checks from one native mapping read."""
        if self.output_snapshot is not None:
            raise RuntimeError("A fabrication generation is already in progress")
        self._require_output_snapshot()
        self._check_corrections(corrections)
        self._validate_ordinary_board()
        filename = self.board.GetFileName()
        parts = tuple(dict(part) for part in self.parent.store.read_all())
        if len({part["reference"] for part in parts}) != len(parts):
            raise ValueError(
                "Board has duplicate component references; repair them before generating"
            )
        source = self._ordinary_mapping(parts)
        geometry = self._physical_geometry_snapshot()

        def validate_source() -> None:
            """Reject changed board mappings or physical placement before output."""
            self._validate_ordinary_board()
            if (
                self.board.GetFileName() != filename
                or self._ordinary_mapping(self.parent.store.read_all()) != source
                or self._physical_geometry_snapshot() != geometry
            ):
                raise RuntimeError(
                    "Board assembly data or placement changed during generation; generate again"
                )

        output = OutputAssemblySnapshot(
            self.prepare_bom(parts), self.prepare_cpl(corrections, parts)
        )
        validate_source()
        self._ordinary_generation = _OrdinaryGeneration(output, validate_source)
        return output

    def end_ordinary_generation(self) -> None:
        """Release ordinary source rows after success, cancellation or failure."""
        self._ordinary_generation = None

    def _artifact_name(
        self,
        prefix: str,
        extension: str,
        variant_name: Optional[str] = None,  # noqa: UP045
    ) -> str:
        """Keep complete readable names and reject unusable filesystem components."""
        stem = Path(self.filename).stem
        variant_name = self.variant_name if variant_name is None else variant_name
        suffix = ""
        if variant_name:
            readable = _variant_filename_label(variant_name)
            if not readable:
                raise ValueError(
                    f"Variant {variant_name!r} has an empty filename label; rename it."
                )
            suffix = f"--variant-{readable}"
        name = f"{prefix}-{stem}{suffix}.{extension}"
        if len(name.encode("utf-8")) > 255:
            raise ValueError(
                f"Output filename exceeds 255 UTF-8 bytes: {name!r}. "
                "Shorten the board or variant name."
            )
        return name

    def get_gerber_zip_path(self) -> str:
        """Return the full path to the generated Gerber ZIP file."""
        return os.path.join(self.outputdir, self._artifact_name("GERBER", "zip"))

    def get_cpl_csv_path(self) -> str:
        """Return the full path to the generated CPL CSV file."""
        return os.path.join(self.outputdir, self._artifact_name("CPL", "csv"))

    def get_bom_csv_path(self) -> str:
        """Return the full path to the generated BOM CSV file."""
        return os.path.join(self.outputdir, self._artifact_name("BOM", "csv"))

    def get_artifact_paths(self) -> dict[str, str]:
        """Return all generated production artifact paths."""
        return {
            "gerber_zip": self.get_gerber_zip_path(),
            "cpl_csv": self.get_cpl_csv_path(),
            "bom_csv": self.get_bom_csv_path(),
        }

    def get_staged_artifact_paths(self) -> dict[str, str]:
        """Return private destinations while a variant generation is in progress."""
        paths = self.get_artifact_paths()
        if self._generation is None:
            return paths
        return {
            key: str(Path(self._generation.directory.name) / Path(path).name)
            for key, path in paths.items()
        }

    def _require_output_snapshot(self) -> None:
        """Never reuse a named filename with the ordinary Default export path."""
        if getattr(self, "variant_name", "") and self.output_snapshot is None:
            raise RuntimeError(
                "Named-variant output requires a new generation snapshot"
            )

    def _physical_geometry_snapshot(self) -> tuple[Any, ...]:
        """Detect origin, pad-center, placement or component changes before publishing."""
        origin = self.board.GetDesignSettings().GetAuxOrigin()
        rows = []
        for fp in sorted(self.board.Footprints(), key=lambda item: item.GetReference()):
            center = self.get_position(fp)
            rows.append(
                (
                    str(fp.GetReference()),
                    str(fp.GetFPID().GetLibItemName()),
                    fp.GetLayer(),
                    self._rotation_for_match(fp, None),
                    center.x,
                    center.y,
                )
            )
        return (origin.x, origin.y, tuple(rows))

    def begin_generation(
        self,
        assembly_snapshot: Any,
        variant_name: str,
        corrections: tuple[AnyCorrection, ...],
        validate_source: Callable[[], None],
    ) -> OutputAssemblySnapshot:
        """Freeze native assembly rows and allocate isolated output staging.

        The caller owns the native snapshot and its validation callback. Matrix
        selection and cache contents are deliberately absent from this API.
        """
        if self.output_snapshot is not None:
            raise RuntimeError("A fabrication generation is already in progress")
        if variant_name not in {variant.name for variant in assembly_snapshot.variants}:
            raise ValueError(f"Output variant is unavailable: {variant_name!r}")
        self._check_corrections(corrections)
        validate_source()
        for prefix, extension in (("GERBER", "zip"), ("CPL", "csv"), ("BOM", "csv")):
            self._artifact_name(prefix, extension, variant_name)
        if variant_name:
            label = unicodedata.normalize(
                "NFC", _variant_filename_label(variant_name).casefold()
            )
            for variant in assembly_snapshot.variants:
                if (
                    variant.name != variant_name
                    and unicodedata.normalize(
                        "NFC", _variant_filename_label(variant.name).casefold()
                    )
                    == label
                ):
                    raise ValueError(
                        f"Variant {variant_name!r} collides with {variant.name!r} "
                        "in output filenames; rename one of them."
                    )
        shared_corrections = resolve_shared_corrections(
            assembly_snapshot, corrections, variant_name
        )
        footprints = {str(fp.GetReference()): fp for fp in self.board.Footprints()}
        origin = self.board.GetDesignSettings().GetAuxOrigin()
        include_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        groups: dict[tuple[str, str, str], list[str]] = {}
        placements = []
        references: set[str] = set()
        for part in sorted(
            assembly_snapshot.for_variant(variant_name), key=lambda part: part.reference
        ):
            if part.reference in references:
                raise ValueError("Output variant has duplicate component references")
            references.add(part.reference)
            if part.component_id not in shared_corrections:
                raise ValueError(
                    f"Default component is unavailable for shared corrections: {part.reference}"
                )
            fp = footprints.get(part.reference)
            if fp is None:
                raise RuntimeError(
                    f"Component {part.reference} disappeared during generation"
                )
            if not part.pop or (not include_without_lcsc and not part.lcsc):
                continue
            package = str(fp.GetFPID().GetLibItemName())
            if part.bom:
                groups.setdefault((part.value, package, part.lcsc), []).append(
                    part.reference
                )
            if part.pos:
                placements.append(
                    self._cpl_row(
                        fp,
                        origin,
                        shared_corrections[part.component_id],
                        (part.reference, part.value, package),
                    )
                )
        bom = tuple(
            (value, ",".join(chunk), package, lcsc, len(chunk))
            for (value, package, lcsc), refs in sorted(groups.items())
            for chunk in split_bom_designators(refs)
        )
        geometry = self._physical_geometry_snapshot()
        validate_source()
        directory = TemporaryDirectory(prefix=".jlcpcb-generation-", dir=self.outputdir)
        gerberdir = str(Path(directory.name) / "gerber")
        try:
            Path(gerberdir).mkdir()
        except OSError:
            self._cleanup_directory(directory)
            raise
        output = OutputAssemblySnapshot(bom, tuple(placements))
        self._generation = _Generation(
            directory, output, validate_source, geometry, self.gerberdir
        )
        self.gerberdir = gerberdir
        self.variant_name = variant_name
        self.corrections = corrections
        self.logger.info(
            "Preparing fabrication for variant %r", variant_name or "Default"
        )
        return output

    def validate_generation(self) -> None:
        """Reject native or physical changes before consuming a captured operation."""
        ordinary = getattr(self, "_ordinary_generation", None)
        if ordinary is not None:
            ordinary.validate_source()
        operation = getattr(self, "_generation", None)
        if operation is not None:
            operation.validate_source()
            if self._physical_geometry_snapshot() != operation.geometry:
                raise RuntimeError(
                    "Board placement or auxiliary origin changed during generation; generate again"
                )

    def publish_generation(self) -> None:
        """Publish artifacts immediately, retaining raw plots until hook cleanup."""
        with self.generation_publication():
            pass

    @contextmanager
    def generation_publication(self) -> Iterator[None]:
        """Retain previous artifacts until the caller's bookkeeping succeeds."""
        operation = self._generation
        if operation is None:
            raise RuntimeError("No staged fabrication generation is in progress")
        self.validate_generation()
        if operation.plot_source_digest is not None:
            validation_path = (
                Path(operation.directory.name) / "validation-source.kicad_pcb"
            )
            if self._serialize_board(validation_path) != operation.plot_source_digest:
                raise RuntimeError(
                    "Board manufacturing data changed after plotting; generate again"
                )
            if (
                self._capture_plot_project_properties(operation.plot_board)
                != operation.plot_project_properties
            ):
                raise RuntimeError(
                    "Project text variables changed after plotting; generate again"
                )
        staged, final = self.get_staged_artifact_paths(), self.get_artifact_paths()
        with artifact_publication(
            tuple((Path(staged[key]), Path(final[key])) for key in final)
        ):
            yield

    def _cleanup_directory(self, directory: TemporaryDirectory) -> None:
        """Report leftover scratch files without masking failure or published success."""
        try:
            directory.cleanup()
        except OSError:
            self.logger.warning(
                "Could not remove temporary fabrication directory %s",
                directory.name,
                exc_info=True,
            )

    def abort_generation(self) -> None:
        """Detach captured state before discarding temporary work, even if cleanup fails."""
        operation, self._generation = self._generation, None
        if operation is not None:
            self.gerberdir = operation.original_gerberdir
            self._cleanup_directory(operation.directory)

    def _board_content(self) -> bytes:
        """Format the live board without synchronizing its project or font files."""
        pcbnew = import_module("pcbnew")
        header = (
            f"(kicad_pcb (version {int(pcbnew.SEXPR_BOARD_FILE_VERSION)}) "
            f'(generator "pcbnew") (generator_version "{pcbnew.GetMajorMinorVersion()}")'
        )
        # BOARD() creates/replaces scripting projects and is unavailable inside
        # PCB Editor. Parse a trusted empty board instead; it has no project.
        reader = pcbnew.STRING_LINE_READER(header + ")", "empty formatting context")
        context = pcbnew.PCB_IO_KICAD_SEXPR().DoLoad(reader, None, None, None, 0)
        self._own_board_copy(context)
        context.SetCopperLayerCount(self.board.GetCopperLayerCount())
        context.SetEnabledLayers(self.board.GetEnabledLayers())
        for layer in self.board.GetEnabledLayers().Seq():
            context.SetLayerName(layer, self.board.GetLayerName(layer))

        # KiCad 10's formatter retains a board for layer names/copper count and
        # group validation. Initialize it on the disposable context because
        # FormatBoardToFormatter also adds/removes embedded fonts. Format then
        # writes the actual source, rebuilding group membership from that source.
        writer = pcbnew.PCB_IO_KICAD_SEXPR()
        discarded = pcbnew.STRING_FORMATTER()
        writer.FormatBoardToFormatter(discarded, context)
        formatter = pcbnew.STRING_FORMATTER()
        writer.SetOutputFormatter(formatter)
        writer.Format(self.board)
        return (header + formatter.GetString() + ")").encode("utf-8")

    def _serialize_board(self, destination: Path) -> str:
        """Capture the loaded board with catchable Python filesystem errors."""
        # KiCad's raw file APIs can abort on an untranslated IO_ERROR.
        content = self._board_content()
        digest = hashlib.sha256(content).hexdigest()
        destination.write_bytes(content)
        return digest

    @staticmethod
    def _own_board_copy(board: Any) -> None:
        """Release a borrowed native board exactly once with its Python proxy."""
        if board.thisown or hasattr(board, "_jlcpcb_release"):
            return
        # SWIG 4's borrowed-to-owned promotion does not retain its type registry,
        # but destruction decrements it. Repeated thisown=True copies eventually
        # break all native wrappers. Keep thisown false and call native deletion
        # explicitly; the callback retains the pointer, never the proxy itself.
        board._jlcpcb_release = weakref.finalize(
            board, type(board).__swig_destroy__, board.this
        )

    @staticmethod
    def _load_board_copy(source: Path, expected_digest: str) -> Any:
        """Parse only the unchanged bytes emitted by this KiCad instance."""
        # Read once so replacement/removal after this check cannot change what
        # the native parser sees. Raw Load also aborts on malformed input; never
        # feed it a damaged temporary file or use this for arbitrary user files.
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise RuntimeError("Temporary board changed before loading; generate again")
        pcbnew = import_module("pcbnew")
        reader = pcbnew.STRING_LINE_READER(content.decode("utf-8"), str(source))
        plugin = pcbnew.PCB_IO_KICAD_SEXPR()
        clone = plugin.DoLoad(reader, None, None, None, 0)
        if clone is not None:
            Fabrication._own_board_copy(clone)
        return clone

    @staticmethod
    def _capture_plot_project_properties(clone: Any) -> tuple[tuple[str, str], ...]:
        """Read live project variables through the clone without changing the source."""
        clone.SynchronizeProperties()
        return tuple(
            sorted(
                (str(key), str(value))
                for key, value in dict(clone.GetProperties()).items()
            )
        )

    def get_report_board(self) -> Any:
        """Use the same explicit variant and board copy for reports and plotting."""
        return self._get_plot_board()

    def _get_plot_board(self) -> Any:
        """Plot a serialized copy in the explicit variant without switching the editor."""
        self._require_output_snapshot()
        self.validate_generation()
        operation = getattr(self, "_generation", None)
        if operation is None:
            return self.board
        if operation.plot_board is None:
            temporary_path = Path(operation.directory.name) / "plot-source.kicad_pcb"
            source_digest = self._serialize_board(temporary_path)
            # Public LoadBoard changes global drawing-sheet and layer contexts.
            # Parsing the verified buffer leaves that editor state intact.
            clone = self._load_board_copy(temporary_path, source_digest)
            if clone is None or not hasattr(clone, "SetCurrentVariant"):
                raise RuntimeError("Installed KiCad cannot isolate variant plotting")
            clone.SetCurrentVariant(self.variant_name)
            if str(clone.GetCurrentVariant()) != self.variant_name:
                raise RuntimeError(
                    f"KiCad could not select plot variant {self.variant_name!r}"
                )
            clone.SetFileName(self.board.GetFileName())
            project = self.board.GetProject()
            if project is not None:
                # Reference-only attachment retains live project text variables
                # without replacing its board settings. Never ClearProject or
                # reattach this clone: that API mutates shared project settings.
                clone.SetProject(project, True)
            self.validate_generation()
            operation.plot_board = clone
            operation.plot_source_digest = source_digest
            operation.plot_project_properties = self._capture_plot_project_properties(
                clone
            )
        return operation.plot_board

    def fill_zones(self, board: Any = None) -> list[str]:
        """Refill copper zones, returning the zone layers that poured no copper."""
        board = self.board if board is None else board
        zones = board.Zones()
        # Refilling mutates the board, reporting does not, so only the refill is optional.
        if self.parent.settings.get("gerber", {}).get("fill_zones", True):
            try:
                if ZONE_FILLER(board).Fill(zones) is False:
                    raise RuntimeError(
                        "Copper zone filling failed. Refill zones and retry."
                    )
            finally:
                Refresh()

        empty_pours = []
        for zone in zones:
            # Rule areas are keepouts, so they never hold copper.
            if zone.GetIsRuleArea():
                continue
            # The filler pours each layer of a zone separately, so check them the same way.
            for layer in zone.GetLayerSet().Seq():
                # Zones on technical layers are graphics, not copper.
                if not IsCopperLayer(layer):
                    continue
                if zone.GetFilledPolysList(layer).Area() > 0:
                    continue
                name = board.GetLayerName(layer)
                empty_pours.append(f"{zone.GetNetname() or 'no net'} on {name}")
        return empty_pours

    def _correction_for_footprint(
        self, footprint: Any, lcsc: str = ""
    ) -> Optional[CorrectionMatch]:  # noqa: UP045
        """Select the same part, reference, value or package rule as the parts table.

        The supplied part number belongs to the same board-derived mapping as
        the BOM, including when a complete export has frozen that mapping.
        """
        return match_correction(
            self.corrections,
            str(footprint.GetReference()),
            str(footprint.GetValue()),
            str(footprint.GetFPID().GetLibItemName()),
            lcsc,
        )

    def fix_rotation(self, footprint: Any, lcsc: str = "") -> float:
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        return self._rotation_for_match(
            footprint, self._correction_for_footprint(footprint, lcsc)
        )

    def _rotation_for_match(
        self,
        footprint: Any,
        match: Optional[CorrectionMatch],  # noqa: UP045
    ) -> float:
        """Apply the already selected rule, including an explicit no-match result."""
        rotation = footprint.GetOrientation().AsDegrees()
        if footprint.GetLayer() != 0:
            # bottom angles need to be mirrored on Y-axis
            rotation = (180 - rotation) % 360
        if match is not None:
            return self.rotate(footprint, rotation, match.correction.rotation)
        return rotation

    def rotate(self, footprint: Any, rotation: float, correction: int) -> float:
        """Calculate the actual correction."""
        # Keep exact whole degrees before adding KiCad's floating point angle.
        rotation = (rotation + correction % 360) % 360
        self.logger.info(
            "Fixed rotation of %s (%s / %s) on %s Layer by %d degrees",
            footprint.GetReference(),
            footprint.GetValue(),
            footprint.GetFPID().GetLibItemName(),
            "Top" if footprint.GetLayer() == 0 else "Bottom",
            correction,
        )
        return rotation

    def reposition(
        self, footprint: Any, position: Any, offset: tuple[float, float]
    ) -> Any:
        """Adjust the position of the footprint, returning the new position as a wxPoint."""
        if offset[0] != 0 or offset[1] != 0:
            rotation = math.radians(self._rotation_for_match(footprint, None))
            x, y = map(FromMM, offset)
            cosine, sine = math.cos(rotation), math.sin(rotation)
            offset_x = x * cosine + y * sine
            offset_y = -x * sine + y * cosine
            if footprint.GetLayer() != 0:
                # mirrored coordinate system needs to be taken into account on the bottom
                offset_x = -offset_x
            self.logger.info(
                "Fixed position of %s (%s / %s) on %s Layer by %f/%f",
                footprint.GetReference(),
                footprint.GetValue(),
                footprint.GetFPID().GetLibItemName(),
                "Top" if footprint.GetLayer() == 0 else "Bottom",
                offset[0],
                offset[1],
            )
            return _checked_position(position.x + offset_x, position.y + offset_y)
        return position

    def fix_position(self, footprint: Any, position: Any, lcsc: str = "") -> Any:
        """Apply the offset from the same selected rule used for rotation."""
        return self._position_for_match(
            footprint, position, self._correction_for_footprint(footprint, lcsc)
        )

    def _position_for_match(
        self,
        footprint: Any,
        position: Any,
        match: Optional[CorrectionMatch],  # noqa: UP045
    ) -> Any:
        """Apply the selected offset without resolving the footprint again."""
        if match is not None:
            return self.reposition(footprint, position, match.correction.offset)
        return position

    def get_position(self, footprint):
        """Calculate position based on center of bounding box."""
        try:
            pads = footprint.Pads()
            bbox = pads[0].GetBoundingBox()
            for pad in pads:
                bbox.Merge(pad.GetBoundingBox())
            return bbox.GetCenter()
        except:
            self.logger.info(
                "WARNING footprint %s: original position used", footprint.GetReference()
            )
            return footprint.GetPosition()

    def generate_geber(self, layer_count: Optional[int] = None) -> None:  # noqa: UP045
        """Generate Gerber files."""
        # inspired by https://github.com/KiCad/kicad-source-mirror/blob/master/demos/python_scripts_examples/gen_gerber_and_drill_files_board.py

        board = self._get_plot_board()
        pctl = PLOT_CONTROLLER(board)
        try:
            self._plot_layers(board, pctl, layer_count)
        finally:
            pctl.ClosePlot()

    def _plot_layers(self, board: Any, pctl: Any, layer_count: Optional[int]) -> None:  # noqa: UP045
        """Configure and plot the selected manufacturing layers."""
        popt = pctl.GetPlotOptions()

        # https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_plot_params.h
        popt.SetOutputDirectory(self.gerberdir)

        # Plot format to Gerber
        # https://github.com/KiCad/kicad-source-mirror/blob/master/include/plotter.h#L67-L78
        popt.SetFormat(1)

        # General Options
        popt.SetPlotValue(
            self.parent.settings.get("gerber", {}).get("plot_values", True)
        )
        popt.SetPlotReference(
            self.parent.settings.get("gerber", {}).get("plot_references", True)
        )

        popt.SetSketchPadsOnFabLayers(False)

        # Gerber Options
        popt.SetUseGerberProtelExtensions(False)

        popt.SetCreateGerberJobFile(False)

        popt.SetSubtractMaskFromSilk(
            self.parent.settings.get("gerber", {}).get("subtract_mask_from_silk", True)
        )

        popt.SetUseAuxOrigin(True)

        # Tented vias or not, selcted by user in settings
        # Only possible via settings in KiCAD < 8.99
        # In KiCAD 8.99 this must be set in the layer settings of KiCAD
        if hasattr(PCB_VIA, "SetPlotViaOnMaskLayer"):
            popt.SetPlotViaOnMaskLayer(
                not self.parent.settings.get("gerber", {}).get("tented_vias", True)
            )

        popt.SetUseGerberX2format(True)

        popt.SetIncludeGerberNetlistInfo(True)

        popt.SetDisableGerberMacros(False)

        popt.SetDrillMarksType(DRILL_MARKS_NO_DRILL_SHAPE)

        popt.SetPlotFrameRef(False)

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = board.GetCopperLayerCount()

        plot_plan_top = [
            ("CuTop", F_Cu, "Top layer"),
            ("SilkTop", F_SilkS, "Silk top"),
            ("MaskTop", F_Mask, "Mask top"),
            ("PasteTop", F_Paste, "Paste top"),
        ]
        # Silkscreen needs no copper under it, so JLCPCB prints a bottom legend
        # even on a board with a single copper layer.
        # https://jlcpcb.com/blog/single-sided-pcb-design
        plot_plan_bottom_silk = ("SilkBottom", B_SilkS, "Silk bottom")
        plot_plan_bottom = [
            ("CuBottom", B_Cu, "Bottom layer"),
            plot_plan_bottom_silk,
            ("MaskBottom", B_Mask, "Mask bottom"),
            ("PasteBottom", B_Paste, "Paste bottom"),
        ]
        # Edge cuts are not a side, so they are added to every plan.
        plot_plan_edges = [("EdgeCuts", Edge_Cuts, "Edges")]

        plot_plan = []

        # Single sided PCB, so bottom copper, mask and paste describe a side that
        # is not manufactured. JLCPCB publishes no layer set for a 1-layer order,
        # so this follows from the board rather than from their spec.
        if layer_count == 1:
            plot_plan = plot_plan_top + [plot_plan_bottom_silk]
        # Double sided PCB
        elif layer_count == 2:
            plot_plan = plot_plan_top + plot_plan_bottom
        # Everything with inner layers
        else:
            plot_plan = (
                plot_plan_top
                + [
                    (
                        f"CuIn{layer}",
                        getattr(import_module("pcbnew"), f"In{layer}_Cu"),
                        f"Inner layer {layer}",
                    )
                    for layer in range(1, layer_count - 1)
                ]
                + plot_plan_bottom
            )

        plot_plan = plot_plan + plot_plan_edges

        # Add all JLC prefixed layers - layers must have "JLC_" in their name
        jlc_layers_to_plot = []
        enabled_layer_ids = list(board.GetEnabledLayers().Seq())
        for enabled_layer_id in enabled_layer_ids:
            layer_name_string = str(board.GetLayerName(enabled_layer_id)).upper()
            if "JLC_" in layer_name_string:
                plotter_info = (layer_name_string, enabled_layer_id, layer_name_string)
                jlc_layers_to_plot.append(plotter_info)
        plot_plan += jlc_layers_to_plot

        # KiCad appends a trimmed layer suffix to the board stem. Its filename
        # sanitization substitutes ASCII characters without changing byte length.
        stem = Path(board.GetFileName()).stem
        for suffix, _layer, description in plot_plan:
            if len(f"{stem}-{suffix.strip()}.gbr".encode()) > 255:
                raise ValueError(
                    f"Gerber filename for {description} exceeds 255 bytes. "
                    "Shorten the board filename or custom layer name before generating."
                )

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        for layer_info in plot_plan:
            popt.SetSkipPlotNPTH_Pads(IsCopperLayer(layer_info[1]))
            pctl.SetLayer(layer_info[1])
            if (
                pctl.OpenPlotfile(layer_info[0], PLOT_FORMAT_GERBER, layer_info[2])
                is False
            ):
                raise RuntimeError(f"Could not open plot file for {layer_info[2]}")
            if pctl.PlotLayer() is False:
                raise RuntimeError(f"Error plotting {layer_info[2]}")
            self.logger.info("Successfully plotted %s", layer_info[2])

    def generate_excellon(self) -> None:
        """Generate Excellon files."""
        board = self._get_plot_board()
        drlwriter = EXCELLON_WRITER(board)
        mirror = False
        minimalHeader = False
        offset = board.GetDesignSettings().GetAuxOrigin()
        mergeNPTH = False
        drlwriter.SetOptions(mirror, minimalHeader, offset, mergeNPTH)
        # JLCPCB asks for Excellon drill data in metric units.
        metric = True
        drlwriter.SetFormat(metric)
        genDrl = True
        genMap = True
        if drlwriter.CreateDrillandMapFilesSet(self.gerberdir, genDrl, genMap) is False:
            raise RuntimeError("Could not generate complete drill and map files")
        self.logger.info("Finished generating Excellon files")

    def zip_gerber_excellon(self, extra_entries: Sequence[ArchiveEntry] = ()) -> Path:
        """Zip Gerber and Excellon files, ready for upload to JLCPCB."""
        self._require_output_snapshot()
        self.validate_generation()
        zip_path = Path(self.get_staged_artifact_paths()["gerber_zip"])
        entries = collect_gerber_entries(Path(self.gerberdir)) + tuple(extra_entries)
        build_archive(zip_path, entries)
        self.logger.info("Finished generating ZIP file %s", zip_path)
        return zip_path

    def generate_cpl(
        self,
        corrections: Optional[tuple[AnyCorrection, ...]] = None,  # noqa: UP045
    ) -> None:
        """Prepare every placement before opening the output file."""
        self.write_cpl(self.prepare_cpl(corrections))

    def prepare_cpl(
        self,
        corrections: Optional[tuple[AnyCorrection, ...]] = None,  # noqa: UP045
        parts: Optional[Iterable[dict[str, Any]]] = None,  # noqa: UP045
    ) -> tuple[tuple[Any, ...], ...]:
        """Capture placement rows from one complete immutable correction set.

        Direct calls read current storage; a supplied preflight tuple stays fixed
        for the operation. Unavailable or unresolved storage is rejected before
        touching the board or opening an existing output file.
        """
        self._require_output_snapshot()
        if self.output_snapshot is not None:
            self.validate_generation()
            return self.output_snapshot.cpl_rows
        if corrections is None:
            snapshot = self.parent.library.read_correction_data()
            corrections = snapshot.corrections
            if corrections is None:
                raise ValueError(
                    f"Corrections are unresolved in the active {snapshot.scope} "
                    f"database ({snapshot.db_path}). Open Corrections Manager "
                    "to repair or retry loading before generating fabrication files."
                )
        self._check_corrections(corrections)
        self.corrections = corrections
        aux_origin = self.board.GetDesignSettings().GetAuxOrigin()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        rows = []
        captured = (
            {part["reference"]: part for part in parts} if parts is not None else None
        )
        footprints = sorted(self.board.Footprints(), key=lambda x: x.GetReference())
        for fp in footprints:
            if get_is_dnp(fp):
                self.logger.info(
                    "Component %s has 'Do not place' enabled: removing from CPL",
                    fp.GetReference(),
                )
                continue
            part = (
                captured.get(fp.GetReference())
                if captured is not None
                else self.parent.store.get_part(fp.GetReference())
            )
            if not part or part["exclude_from_pos"] == 1 or part.get("is_dnp", False):
                continue
            if not add_without_lcsc and not part["lcsc"]:
                continue
            match = self._correction_for_footprint(fp, part["lcsc"])
            rows.append(
                self._cpl_row(
                    fp,
                    aux_origin,
                    match,
                    (part["reference"], part["value"], part["footprint"]),
                )
            )
        return tuple(rows)

    def _cpl_row(
        self,
        footprint: Any,
        origin: Any,
        match: Optional[CorrectionMatch],  # noqa: UP045
        identity: tuple[str, str, str],
    ) -> tuple[Any, ...]:
        """Format reference/value/package with the shared placement transformations."""
        try:
            center = self.get_position(footprint)
            # Subtract in Python before native coordinate arithmetic can wrap.
            position = SimpleNamespace(x=center.x - origin.x, y=center.y - origin.y)
            position = self._position_for_match(footprint, position, match)
            position = _checked_position(position.x, position.y)
            return (
                *identity,
                # Six decimal millimetres retain KiCad's nanometre resolution.
                f"{ToMM(position.x):.6f}",
                f"{ToMM(position.y) * -1:.6f}",
                self._rotation_for_match(footprint, match),
                "top" if footprint.GetLayer() == 0 else "bottom",
            )
        except (OverflowError, ValueError) as error:
            source = (
                f"correction {match.correction.key!r}" if match else "no correction"
            )
            raise ValueError(
                f"Cannot generate CPL for {identity[0]} ({source}): {error}"
            ) from error

    @staticmethod
    def _check_corrections(corrections: tuple[AnyCorrection, ...]) -> None:
        """Reject unresolved or mutable rules before capturing manufacturing rows."""
        if not isinstance(corrections, tuple) or any(
            not isinstance(correction, (Correction, LcscCorrection))
            for correction in corrections
        ):
            raise TypeError(
                "Expected an immutable tuple of Correction or LcscCorrection values"
            )

    def write_cpl(self, rows: tuple[tuple[Any, ...], ...]) -> None:
        """Validate captured source before writing its prepared placements."""
        self._require_output_snapshot()
        self.validate_generation()
        cpl_path = self.get_staged_artifact_paths()["cpl_csv"]
        with open(cpl_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            writer.writerows(rows)
        self.logger.info("Finished generating CPL file %s", cpl_path)

    def generate_bom(self) -> None:
        """Prepare every BOM row before opening the output file."""
        self.write_bom(self.prepare_bom())

    def prepare_bom(
        self,
        parts: Optional[Iterable[dict[str, Any]]] = None,  # noqa: UP045
    ) -> tuple[tuple[Any, ...], ...]:
        """Resolve current BOM groups before an output file can be truncated."""
        self._require_output_snapshot()
        if self.output_snapshot is not None:
            self.validate_generation()
            return self.output_snapshot.bom_rows
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        footprints = {fp.GetReference(): fp for fp in self.board.Footprints()}
        rows = []
        groups = (
            self.parent.store.read_bom_parts(parts)
            if parts is not None
            else self.parent.store.read_bom_parts()
        )
        for part in groups:
            if not add_without_lcsc and not part["lcsc"]:
                self.logger.info(
                    "Component group %s has no assigned LCSC: removing from BOM",
                    part["refs"],
                )
                continue
            components = []
            for reference in part["refs"].split(","):
                fp = footprints.get(reference)
                if fp is None or get_is_dnp(fp):
                    self.logger.info(
                        "Component %s is absent or not populated: removing from BOM",
                        reference,
                    )
                    continue
                components.append(reference)
            rows.extend(
                (
                    part["value"],
                    ",".join(chunk),
                    part["footprint"],
                    part["lcsc"],
                    len(chunk),
                )
                for chunk in split_bom_designators(components)
            )
        return tuple(rows)

    def write_bom(self, rows: tuple[tuple[Any, ...], ...]) -> None:
        """Validate captured source before writing its prepared BOM rows."""
        self._require_output_snapshot()
        self.validate_generation()
        bom_path = self.get_staged_artifact_paths()["bom_csv"]
        with open(bom_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Comment", "Designator", "Footprint", "LCSC", "Quantity"])
            writer.writerows(rows)
        self.logger.info("Finished generating BOM file %s", bom_path)

    def get_part_consistency_warnings(self) -> str:
        """Check the plausibility of the parts, there should be just one value per LCSC number.

        Returns an empty sting if all parts are ok, otherwise a otherwise a overview of parts that share a LCSC number but have different values.
        """
        lcsc_numbers: dict[str, dict[str, list[str]]] = {}
        self.validate_generation()
        if self.output_snapshot is None:
            parts = self.parent.store.read_bom_parts()
        else:
            parts = [
                {"value": row[0], "refs": row[1], "lcsc": row[3]}
                for row in self.output_snapshot.bom_rows
            ]
        for item in parts:
            if not item["lcsc"]:
                continue
            values = lcsc_numbers.setdefault(item["lcsc"], {})
            values.setdefault(item["value"], []).extend(item["refs"].split(","))
        filtered = {key: value for key, value in lcsc_numbers.items() if len(value) > 1}
        result = ""
        for lcsc, items in filtered.items():
            result += f"{lcsc}:\n"
            for value, references in items.items():
                result += f"  - {','.join(references)} -> {value}\n"
        return result
