"""Build one immutable capture/report model for previews, Excel, and HTML."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import struct
import tempfile
from typing import TYPE_CHECKING, Any, Optional, Protocol

from .capture_cache import OwnedCaptureCache, capture_key
from .matching import analyze, validate_review
from .model import (
    Analysis,
    BoardSnapshot,
    Config,
    Section,
    ValidationError,
    positive_decimal,
    resolved_layer_settings,
)

if TYPE_CHECKING:
    from .spacing_checks import SpacingCheck, SpacingContext
    from .spacing_review import SpacingReviewEntry, SpacingReviewImage
    from .width_checks import WidthCheck

MAX_ROWS = 1000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
IMAGE_WIDTH = 800
IMAGE_HEIGHT = 420
KINDS = {
    "differential": "Differential Pair (Non coplanar)",
    "differential_coplanar": "Differential Pair (Coplanar)",
    "single_ended": "Single Ended (Non coplanar)",
    "single_ended_coplanar": "Single Ended (Coplanar)",
}


@dataclass(frozen=True)
class CapturedImage:
    """Own a bounded native PNG once; consumers never reopen a mutable image path.

    The native renderer/decoder produces the pixels. This boundary checks the
    expected container and dimensions, not a second implementation of PNG.
    """

    data: bytes
    viewport_nm: Optional[tuple[float, float, float, float]] = field(
        default=None, compare=False
    )
    original: Optional["CapturedImage"] = field(default=None, compare=False)
    width: int = field(init=False)
    height: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the PNG container and record its dimensions and digest."""
        if not isinstance(self.data, bytes):
            raise TypeError("A capture requires immutable PNG bytes.")
        if self.original is not None:
            if not isinstance(self.original, CapturedImage):
                raise TypeError("An original comparison requires a captured image.")
            if self.original.original is not None:
                raise ValidationError(
                    "An original comparison cannot contain another comparison."
                )
        if self.viewport_nm is not None:
            from .spacing_overlays import valid_viewport

            if not valid_viewport(self.viewport_nm):
                raise ValidationError("The impedance capture viewport is invalid.")
        if len(self.data) > MAX_IMAGE_BYTES:
            raise ValidationError(
                "The impedance capture exceeds the 10 MiB size limit."
            )
        if (
            len(self.data) < 33
            or self.data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        ):
            raise ValidationError("The impedance capture is not a native PNG image.")
        width, height = struct.unpack_from(">II", self.data, 16)
        if not (0 < width <= MAX_IMAGE_DIMENSION and 0 < height <= MAX_IMAGE_DIMENSION):
            raise ValidationError(
                "The impedance capture dimensions must be between 1 and 4096 pixels."
            )
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "sha256", hashlib.sha256(self.data).hexdigest())

    @property
    def byte_size(self) -> int:
        """Include the optional comparison in capture and export memory limits."""
        return len(self.data) + (
            len(self.original.data) if self.original is not None else 0
        )

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        viewport_nm: Optional[tuple[float, float, float, float]] = None,
        original: Optional["CapturedImage"] = None,
    ) -> "CapturedImage":
        """Capture the exact bytes that the preview and both reports will share."""
        return cls(data, viewport_nm=viewport_nm, original=original)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        viewport_nm: Optional[tuple[float, float, float, float]] = None,
        original: Optional["CapturedImage"] = None,
    ) -> "CapturedImage":
        """Read one locally generated PNG with a bounded allocation."""
        try:
            with Path(path).open("rb") as source:
                return cls(
                    source.read(MAX_IMAGE_BYTES + 1),
                    viewport_nm=viewport_nm,
                    original=original,
                )
        except OSError as error:
            raise ValidationError(f"Cannot read impedance capture: {path}.") from error


def load_rendered_capture(
    path: Path,
    renderer: Any,
    section: Section,
    *,
    width_px: int = IMAGE_WIDTH,
    height_px: int = IMAGE_HEIGHT,
) -> CapturedImage:
    """Attach the exact renderer transform without modifying captured pixels."""
    viewport_getter = getattr(renderer, "viewport", None)
    viewport = (
        viewport_getter(section, width_px=width_px, height_px=height_px)
        if callable(viewport_getter)
        else None
    )
    return CapturedImage.load(path, viewport_nm=viewport)


def load_spacing_capture(
    path: Path,
    renderer: Any,
    entry: "SpacingReviewEntry",
    *,
    include_original: bool = True,
) -> CapturedImage:
    """Own a combined image and optional native original without a false transform.

    Several panels cannot share one board-to-image viewport. Only the unannotated
    original has the entry's physical detail transform. Its pixels are a read-only
    comparison, independent of the exact combined PNG that requires approval.
    """
    capture = CapturedImage.load(path)
    if (capture.width, capture.height) != (entry.width_px, entry.height_px):
        raise ValidationError("The spacing capture has unexpected dimensions.")
    if not include_original:
        return capture
    destination = Path(path).with_name(f"{Path(path).stem}-original.png")
    return attach_spacing_original(capture, renderer, entry, destination)


def attach_spacing_original(
    capture: CapturedImage,
    renderer: Any,
    entry: "SpacingReviewEntry",
    destination: Path,
) -> CapturedImage:
    """Create HTML's independent comparison only when that consumer needs it."""
    if capture.original is not None:
        validate_spacing_original(capture.original, entry)
        return capture
    render_original = getattr(renderer, "render_spacing_original", None)
    if not callable(render_original):
        return capture
    result = Path(render_original(entry, destination))
    if result.resolve() != destination.resolve():
        raise ValidationError(
            "The spacing renderer did not create its expected original PNG."
        )
    original = CapturedImage.load(destination, viewport_nm=entry.viewport_nm)
    validate_spacing_original(original, entry)
    return CapturedImage.from_bytes(capture.data, original=original)


def validate_spacing_original(
    original: CapturedImage, entry: "SpacingReviewEntry"
) -> None:
    """Check detail dimensions and transform even when the comparison is cached."""
    composition = getattr(entry, "composition", None)
    expected = (
        tuple(composition.detail_rect[2:])
        if composition is not None
        else (entry.width_px, entry.height_px)
    )
    if (
        original.width,
        original.height,
    ) != expected or original.viewport_nm != entry.viewport_nm:
        raise ValidationError(
            "The original spacing comparison has unexpected dimensions."
        )


@dataclass(frozen=True)
class CaptureEvidence:
    """Current capture hashes, including failures, without retaining PNG bytes."""

    current: bool
    hashes: dict[Section, Optional[str]]


@dataclass(frozen=True)
class SpacingCaptureEvidence:
    """Verified diagnostic hashes kept separate from manufacturing image evidence."""

    current: bool
    hashes: dict["SpacingReviewEntry", Optional[str]]


@dataclass(frozen=True)
class ReportRow:
    """One section and image, shared unchanged by the Excel and HTML writers."""

    specification: str
    nets: tuple[str, ...]
    net_class: str
    copper_layers: tuple[str, ...]
    signal_layer: str
    reference_layers: tuple[str, ...]
    kind: str
    width_nm: int
    spacing_nm: Optional[int]
    target_ohms: str
    image: CapturedImage
    ground_gap_nm: Optional[int] = None
    last_viewed_at_utc: str = ""
    last_viewed_current: bool = False
    layer_approved_at_utc: str = ""
    layer_approval_current: bool = False
    width_check: Optional["WidthCheck"] = None
    spacing_check: Optional["SpacingCheck"] = None

    def __post_init__(self) -> None:
        """Validate the immutable image, board layers, and impedance dimensions."""
        if self.kind not in KINDS or not isinstance(self.image, CapturedImage):
            raise ValidationError(
                "A report row requires a known impedance type and captured image."
            )
        for names in (self.nets, self.copper_layers, self.reference_layers):
            if not isinstance(names, tuple) or any(
                not isinstance(name, str) for name in names
            ):
                raise TypeError(
                    "Report net and layer names must be immutable tuples of text."
                )
        if (
            not self.reference_layers
            or self.signal_layer not in self.copper_layers
            or len(set(self.copper_layers)) != len(self.copper_layers)
            or any(
                layer not in self.copper_layers or layer == self.signal_layer
                for layer in self.reference_layers
            )
            or len(set(self.reference_layers)) != len(self.reference_layers)
        ):
            raise ValidationError(
                "Report signal and reference layers must belong to the board's copper stack."
            )
        _validate_dimensions(
            self.width_nm, self.spacing_nm, self.ground_gap_nm, self.target_ohms
        )
        if self.kind.startswith("differential") and self.spacing_nm is None:
            raise ValidationError("Differential impedance types require pair spacing.")
        if self.kind.endswith("_coplanar") and self.ground_gap_nm is None:
            raise ValidationError("Coplanar impedance types require a ground gap.")

    @property
    def physical_signal_layer(self) -> str:
        """Name the signal layer in JLCPCB's physical L1…Ln convention."""
        return f"L{self.copper_layers.index(self.signal_layer) + 1}"

    @property
    def physical_reference_layers(self) -> tuple[str, ...]:
        """Keep both reference planes for an internal signal layer."""
        return tuple(
            f"L{self.copper_layers.index(layer) + 1}" for layer in self.reference_layers
        )


def _validate_dimensions(
    width: int, spacing: Optional[int], ground_gap: Optional[int], target_ohms: str
) -> None:
    """Reject unrepresentable manufacturing values before fabrication or writing."""
    if type(width) is not int or not 0 < width <= 10**12:
        raise ValidationError(
            "The actual trace width must be a positive bounded nanometre integer."
        )
    for value in (spacing, ground_gap):
        if value is not None and (type(value) is not int or not 0 < value <= 10**12):
            raise ValidationError(
                "Report gaps must be positive bounded nanometre integers."
            )
    if not -12 <= positive_decimal(target_ohms, "target impedance").adjusted() <= 12:
        raise ValidationError("Target impedance must be between 1e-12 and 1e13 ohms.")


def validate_report_rows(rows: Sequence[ReportRow]) -> None:
    """Bound complete outputs without revalidating or decoding each capture."""
    if not 0 < len(rows) <= MAX_ROWS or any(
        not isinstance(row, ReportRow) for row in rows
    ):
        raise ValidationError(
            f"An impedance report requires between 1 and {MAX_ROWS} captured rows."
        )
    if sum(len(row.image.data) for row in rows) > MAX_TOTAL_IMAGE_BYTES:
        raise ValidationError("Position images exceed the 128 MiB total size limit.")


def validate_spacing_review_images(
    images: Sequence["SpacingReviewImage"], *, manufacturing_bytes: int = 0
) -> None:
    """Bound diagnostic payloads separately and together with workbook captures."""
    from .spacing_review import (
        MAX_SPACING_REVIEW_BYTES,
        MAX_SPACING_REVIEW_IMAGES,
        SpacingReviewEntry,
        SpacingReviewImage,
    )

    if len(images) > MAX_SPACING_REVIEW_IMAGES or any(
        not isinstance(image, SpacingReviewImage)
        or not isinstance(image.entry, SpacingReviewEntry)
        or not isinstance(image.capture, CapturedImage)
        for image in images
    ):
        raise ValidationError(
            f"Spacing review requires at most {MAX_SPACING_REVIEW_IMAGES} diagnostic images."
        )
    diagnostic_bytes = sum(image.capture.byte_size for image in images)
    if diagnostic_bytes > MAX_SPACING_REVIEW_BYTES:
        raise ValidationError("Spacing review images exceed the 128 MiB size limit.")
    if diagnostic_bytes + manufacturing_bytes > MAX_TOTAL_IMAGE_BYTES:
        raise ValidationError(
            "Manufacturing and spacing review images exceed the 128 MiB combined size limit."
        )


class Renderer(Protocol):
    """Render a section using the existing native renderer contract."""

    def render(
        self, section: Section, destination: Path, width_px: int, height_px: int
    ) -> Path:
        """Return the generated native PNG path."""
        ...


RendererFactory = Callable[[Any, Any], Renderer]
WorkbookWriter = Callable[[Sequence[ReportRow], Path], Path]


@dataclass(frozen=True)
class ExportPlan:
    """Carry the exact configuration and reviewed geometry accepted by preflight."""

    config: Config
    snapshot: BoardSnapshot
    analysis: Analysis
    sections: tuple[Section, ...]
    spacing_context: Optional["SpacingContext"] = field(
        default=None, compare=False, repr=False
    )


@dataclass(frozen=True)
class ExportArtifacts:
    """Name both completed reports that must be published together."""

    workbook: Path
    html_report: Path
    appearance: Optional[object] = None


def report_rows(
    plan: ExportPlan, images: Sequence[CapturedImage]
) -> tuple[ReportRow, ...]:
    """Join manufacturing intent, current review records, and captured pixels once."""
    from .review_tracking import (
        capture_fingerprint,
        find_image,
        find_layer,
        layer_approval_current,
        source_fingerprint,
    )
    from .spacing_checks import spacing_checks_for_sections
    from .width_checks import width_check

    if len(images) != len(plan.sections):
        raise ValidationError("Every reviewed impedance section requires one image.")
    spacing_by_section = spacing_checks_for_sections(
        plan.config, plan.snapshot, plan.sections, context=plan.spacing_context
    )
    specifications = {spec.spec_id: spec for spec in plan.config.specifications}
    tracking = plan.config.review_tracking
    source_digest = source_fingerprint(plan.snapshot)
    capture_digests = [
        capture_fingerprint(
            source_digest, specifications[section.spec_id], section, image.sha256
        )
        for section, image in zip(plan.sections, images)
    ]
    current_captures = {
        section.section_id: digest
        for section, digest in zip(plan.sections, capture_digests)
    }
    required_ids = set(current_captures)
    layer_current: dict[tuple[str, str], bool] = {}
    rows = []
    for section, image, digest in zip(plan.sections, images, capture_digests):
        spec = specifications[section.spec_id]
        settings = resolved_layer_settings(spec, section.layer)
        viewed = find_image(tracking, spec.spec_id, section.layer, section.section_id)
        approved = find_layer(tracking, spec.spec_id, section.layer)
        layer_key = (spec.spec_id, section.layer)
        if layer_key not in layer_current:
            layer_current[layer_key] = layer_approval_current(
                tracking,
                source_digest,
                spec,
                section.layer,
                plan.analysis.sections,
                stackup=plan.config.stackup,
                width_results=plan.config.width_results,
                capture_digests=current_captures,
                required_section_ids=required_ids,
            )
        rows.append(
            ReportRow(
                specification=spec.label or f"{spec.target_ohms} Ω",
                nets=section.net_names,
                net_class=spec.net_class,
                copper_layers=plan.snapshot.layers,
                signal_layer=section.layer,
                reference_layers=settings.reference_layers,
                kind=spec.kind,
                width_nm=section.width_nm,
                spacing_nm=settings.spacing_nm,
                target_ohms=spec.target_ohms,
                image=image,
                ground_gap_nm=settings.ground_gap_nm,
                last_viewed_at_utc=viewed.viewed_at_utc if viewed is not None else "",
                last_viewed_current=viewed is not None
                and viewed.capture_digest == digest,
                layer_approved_at_utc=approved.approved_at_utc
                if approved is not None
                else "",
                layer_approval_current=layer_current[layer_key],
                width_check=width_check(
                    plan.config, spec, section.layer, section.width_nm
                ),
                spacing_check=spacing_by_section.get(section.section_id),
            )
        )
    return tuple(rows)


def prepare(
    config: Config,
    snapshot: BoardSnapshot,
    layer_count: Optional[int] = None,
    *,
    spacing_context: Optional["SpacingContext"] = None,
) -> Optional[ExportPlan]:
    """Require reviewed current geometry and a usable template before fabrication."""
    if not isinstance(config, Config) or type(config.enabled) is not bool:
        raise ValidationError("Invalid impedance configuration.")
    if not config.enabled:
        return None
    analysis = analyze(config, snapshot)
    validate_review(config, analysis)
    if layer_count is not None and (
        type(layer_count) is not int or layer_count != len(snapshot.layers)
    ):
        raise ValidationError(
            "The fabrication layer count must match the board's copper stack when controlled impedance is enabled."
        )
    included = set(config.included_section_ids)
    sections = tuple(
        section for section in analysis.sections if section.section_id in included
    )
    if len(sections) > MAX_ROWS:
        raise ValidationError(
            f"The impedance form supports at most {MAX_ROWS} reviewed sections."
        )
    from .workbook import validate_template

    specifications = {spec.spec_id: spec for spec in config.specifications}
    for section in sections:
        spec = specifications[section.spec_id]
        settings = resolved_layer_settings(spec, section.layer)
        _validate_dimensions(
            section.width_nm,
            settings.spacing_nm,
            settings.ground_gap_nm,
            spec.target_ohms,
        )
    from .review_tracking import find_spacing_image, spacing_image_current
    from .spacing_checks import SpacingContext
    from .spacing_review import spacing_review_entries

    if spacing_context is None or spacing_context.snapshot is not snapshot:
        spacing_context = SpacingContext(snapshot)
    entries = spacing_review_entries(
        config, snapshot, analysis.sections, context=spacing_context
    )
    for entry in entries:
        record = find_spacing_image(
            config.review_tracking, entry.spec_id, entry.layer, entry.view_id
        )
        if (
            record is None
            or record.issue_id != entry.issue_id
            or not spacing_image_current(record, entry.input_digest, approved=True)
        ):
            raise ValidationError(
                "Spacing review images require individual approval. Open Configure "
                "to review and approve every spacing image before generation."
            )
    validate_template()
    return ExportPlan(config, snapshot, analysis, sections, spacing_context)


def validate_current(
    plan: ExportPlan,
    config: Config,
    snapshot: BoardSnapshot,
    layer_count: Optional[int] = None,
) -> None:
    """Reject changed intent or geometry immediately before publishing artifacts."""
    current = prepare(
        config, snapshot, layer_count, spacing_context=plan.spacing_context
    )
    if current is None or (
        current.config != plan.config
        or current.analysis.digest != plan.analysis.digest
        or current.sections != plan.sections
        or current.snapshot.layers != plan.snapshot.layers
    ):
        raise ValidationError(
            "The board or controlled-impedance configuration changed during generation. Review the sections and generate again."
        )


def export_reports(
    plan: ExportPlan,
    board: Any,
    pcbnew_module: Any,
    scratch_dir: Path,
    renderer_factory: Optional[RendererFactory] = None,
    writer: Optional[WorkbookWriter] = None,
    appearance_context: Optional[Callable[[], object]] = None,
    capture_cache: Optional[OwnedCaptureCache] = None,
    capture_identity: Optional[tuple[str, Optional[str]]] = None,
    source_context: Optional[Callable[[], BoardSnapshot]] = None,
) -> ExportArtifacts:
    """Render once and create both reports before the caller publishes the ZIP."""
    from .html_report import OUTPUT_FILENAME as HTML_FILENAME, write_html_report
    from .palette import read_theme_context
    from .render import SectionRenderer
    from .review_tracking import (
        capture_fingerprint,
        expected_capture_digest,
        find_spacing_image,
        source_fingerprint,
        spacing_capture_fingerprint,
        spacing_image_current,
    )
    from .spacing_checks import SpacingContext
    from .spacing_review import (
        MAX_SPACING_REVIEW_IMAGES,
        SpacingReviewImage,
        spacing_review_entries,
    )
    from .workbook import OUTPUT_FILENAME, write_workbook

    context = plan.spacing_context or SpacingContext(plan.snapshot)
    plan = ExportPlan(plan.config, plan.snapshot, plan.analysis, plan.sections, context)
    validate_current(plan, plan.config, plan.snapshot)
    spacing_entries = spacing_review_entries(
        plan.config, plan.snapshot, plan.analysis.sections, context=context
    )
    if len(spacing_entries) > MAX_SPACING_REVIEW_IMAGES:
        raise ValidationError(
            f"Spacing review requires at most {MAX_SPACING_REVIEW_IMAGES} diagnostic images."
        )
    scratch_dir = Path(scratch_dir)
    if not scratch_dir.is_dir():
        raise ValidationError("The impedance export scratch directory does not exist.")
    if appearance_context is None and renderer_factory is None:
        appearance_context = lambda: read_theme_context(pcbnew_module)
    appearance = appearance_context() if appearance_context is not None else None
    if capture_cache is not None and (
        capture_identity is None
        or not isinstance(capture_identity[0], str)
        or not capture_identity[0].strip()
        or source_context is None
        or appearance_context is None
    ):
        raise ValidationError(
            "Owned captures require live source and appearance checks."
        )
    if source_context is not None and source_context() != plan.snapshot:
        raise ValidationError(
            "The report output changed before its captures were rendered."
        )
    renderer: Optional[Renderer] = None

    def current_renderer() -> Renderer:
        """Do not create native plots when all required PNGs are already owned."""
        nonlocal renderer
        if renderer is None:
            renderer = (renderer_factory or SectionRenderer)(board, pcbnew_module)
        return renderer

    def owned_key(item: object, dimensions: tuple[int, int]) -> Any:
        """Sharing requires an independently verified board and output identity."""
        if capture_cache is None or capture_identity is None:
            return None
        return capture_key(
            capture_cache,
            capture_identity[0],
            plan.snapshot,
            appearance,
            capture_identity[1],
            item,
            dimensions=dimensions,
        )

    images = []
    spacing_images = []
    staged_captures = []
    total_bytes = 0
    try:
        for index, section in enumerate(plan.sections, 1):
            destination = scratch_dir / f"impedance-section-{index:04d}.png"
            key = owned_key(section, (IMAGE_WIDTH, IMAGE_HEIGHT))
            capture = capture_cache.get(key) if key is not None else None
            if capture is None:
                active_renderer = current_renderer()
                result = Path(
                    active_renderer.render(
                        section,
                        destination,
                        width_px=IMAGE_WIDTH,
                        height_px=IMAGE_HEIGHT,
                    )
                )
                if result.resolve() != destination.resolve():
                    raise ValidationError(
                        "The impedance renderer did not create its expected PNG."
                    )
                capture = load_rendered_capture(
                    result,
                    active_renderer,
                    section,
                    width_px=IMAGE_WIDTH,
                    height_px=IMAGE_HEIGHT,
                )
            if key is not None:
                staged_captures.append((key, capture))
            total_bytes += len(capture.data)
            if total_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise ValidationError(
                    "Position images exceed the 128 MiB total size limit."
                )
            images.append(capture)
        if spacing_entries:
            # Diagnostic PNG paths have no manufacturing identity or report-row
            # slot. Their bytes are owned before the temporary directory closes.
            with tempfile.TemporaryDirectory(
                prefix=".impedance-spacing-", dir=scratch_dir
            ) as directory:
                for index, entry in enumerate(spacing_entries, 1):
                    destination = Path(directory) / f"spacing-{index:04d}.png"
                    key = owned_key(entry, (entry.width_px, entry.height_px))
                    capture = capture_cache.get(key) if key is not None else None
                    if capture is None:
                        active_renderer = current_renderer()
                        render_spacing = getattr(
                            active_renderer, "render_spacing", None
                        )
                        if not callable(render_spacing):
                            raise ValidationError(
                                "The renderer cannot capture spacing review images. Open Configure "
                                "to review the spacing images and generate again."
                            )
                        result = Path(
                            render_spacing(
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
                            destination, active_renderer, entry, include_original=False
                        )
                    if capture.original is None:
                        capture = attach_spacing_original(
                            capture,
                            current_renderer(),
                            entry,
                            destination.with_name(f"{destination.stem}-original.png"),
                        )
                    else:
                        validate_spacing_original(capture.original, entry)
                    if key is not None:
                        staged_captures.append((key, capture))
                    if (capture.width, capture.height) != (
                        entry.width_px,
                        entry.height_px,
                    ):
                        raise ValidationError(
                            "The spacing review capture has changed dimensions. Open "
                            "Configure to review the spacing images and generate again."
                        )
                    record = find_spacing_image(
                        plan.config.review_tracking,
                        entry.spec_id,
                        entry.layer,
                        entry.view_id,
                    )
                    if (
                        record is None
                        or record.issue_id != entry.issue_id
                        or not spacing_image_current(
                            record,
                            entry.input_digest,
                            spacing_capture_fingerprint(
                                entry.input_digest, capture.sha256
                            ),
                            approved=True,
                        )
                    ):
                        raise ValidationError(
                            "A spacing review image changed since approval. Open Configure "
                            "to review and approve the spacing images and generate again."
                        )
                    spacing_images.append(SpacingReviewImage(entry, capture))
                    validate_spacing_review_images(
                        spacing_images, manufacturing_bytes=total_bytes
                    )
    finally:
        close = getattr(renderer, "close", None) if renderer is not None else None
        if callable(close):
            close()
    if appearance_context is not None and appearance_context() != appearance:
        raise ValidationError(
            "The PCB appearance changed during impedance capture. Open Configure "
            "to review the images and generate again."
        )
    if source_context is not None and source_context() != plan.snapshot:
        raise ValidationError(
            "The report output changed while its captures were rendered."
        )
    specifications = {spec.spec_id: spec for spec in plan.config.specifications}
    source_digest = source_fingerprint(plan.snapshot)
    for section, image in zip(plan.sections, images):
        spec = specifications[section.spec_id]
        expected = expected_capture_digest(
            plan.config.review_tracking,
            source_digest,
            spec,
            section,
            plan.analysis.sections,
            stackup=plan.config.stackup,
            width_results=plan.config.width_results,
            workbook_captures=plan.config.reviewed_captures,
        )
        if expected is not None and expected != capture_fingerprint(
            source_digest, spec, section, image.sha256
        ):
            raise ValidationError(
                "An impedance image changed since review. Open Configure to "
                "review the images and generate again."
            )
    if capture_cache is not None:
        for key, image in staged_captures:
            capture_cache.put(key, image)
    rows = report_rows(plan, images)
    workbook = scratch_dir / OUTPUT_FILENAME
    html_report = scratch_dir / HTML_FILENAME
    filename = getattr(board, "GetFileName", None)
    board_name = Path(filename()).name if callable(filename) else "PCB"
    outputs = (
        ((writer or write_workbook)(rows, workbook), workbook),
        (
            write_html_report(
                rows,
                html_report,
                board_name=board_name or "Untitled board",
                stackup=plan.config.stackup,
                spacing_reviews=tuple(spacing_images),
                review_tracking=plan.config.review_tracking,
            ),
            html_report,
        ),
    )
    for result, expected in outputs:
        if Path(result).resolve() != expected.resolve() or not expected.is_file():
            raise ValidationError(
                "The impedance writer did not create its expected report."
            )
    return ExportArtifacts(workbook, html_report, appearance)
