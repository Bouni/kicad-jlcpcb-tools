"""Build one immutable capture/report model for previews, Excel, and HTML."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import struct
from typing import TYPE_CHECKING, Any, Optional, Protocol

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
    width: int = field(init=False)
    height: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the PNG container and record its dimensions and digest."""
        if not isinstance(self.data, bytes):
            raise TypeError("A capture requires immutable PNG bytes.")
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

    @classmethod
    def from_bytes(cls, data: bytes) -> "CapturedImage":
        """Capture the exact bytes that the preview and both reports will share."""
        return cls(data)

    @classmethod
    def load(cls, path: Path) -> "CapturedImage":
        """Read one locally generated PNG with a bounded allocation."""
        try:
            with Path(path).open("rb") as source:
                return cls(source.read(MAX_IMAGE_BYTES + 1))
        except OSError as error:
            raise ValidationError(f"Cannot read impedance capture: {path}.") from error


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


@dataclass(frozen=True)
class ExportArtifacts:
    """Name both completed reports that must be published together."""

    workbook: Path
    html_report: Path


def report_rows(
    plan: ExportPlan, images: Sequence[CapturedImage]
) -> tuple[ReportRow, ...]:
    """Join manufacturing intent, current review records, and captured pixels once."""
    from .review_tracking import (
        capture_fingerprint,
        find_image,
        find_layer,
        layer_fingerprint,
        source_fingerprint,
    )
    from .width_checks import width_check

    if len(images) != len(plan.sections):
        raise ValidationError("Every reviewed impedance section requires one image.")
    specifications = {spec.spec_id: spec for spec in plan.config.specifications}
    tracking = plan.config.review_tracking
    source_digest = source_fingerprint(plan.snapshot)
    capture_digests = [
        capture_fingerprint(
            source_digest, specifications[section.spec_id], section, image.sha256
        )
        for section, image in zip(plan.sections, images)
    ]
    layer_captures: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for section, digest in zip(plan.sections, capture_digests):
        layer_captures.setdefault((section.spec_id, section.layer), []).append(
            (section.section_id, digest)
        )
    layer_current: dict[tuple[str, str], bool] = {}
    rows = []
    for section, image, digest in zip(plan.sections, images, capture_digests):
        spec = specifications[section.spec_id]
        settings = resolved_layer_settings(spec, section.layer)
        viewed = find_image(tracking, spec.spec_id, section.layer, section.section_id)
        approved = find_layer(tracking, spec.spec_id, section.layer)
        layer_key = (spec.spec_id, section.layer)
        if layer_key not in layer_current:
            settings_digest = layer_fingerprint(
                source_digest,
                spec,
                section.layer,
                tuple(
                    candidate
                    for candidate in plan.analysis.sections
                    if candidate.spec_id == spec.spec_id
                    and candidate.layer == section.layer
                ),
                stackup=plan.config.stackup,
                width_results=plan.config.width_results,
            )
            layer_current[layer_key] = (
                approved is not None
                and approved.settings_digest == settings_digest
                and tuple(sorted(approved.captures))
                == tuple(sorted(layer_captures[layer_key]))
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
            )
        )
    return tuple(rows)


def prepare(
    config: Config, snapshot: BoardSnapshot, layer_count: Optional[int] = None
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
    validate_template()
    return ExportPlan(config, snapshot, analysis, sections)


def validate_current(
    plan: ExportPlan,
    config: Config,
    snapshot: BoardSnapshot,
    layer_count: Optional[int] = None,
) -> None:
    """Reject changed intent or geometry immediately before publishing artifacts."""
    current = prepare(config, snapshot, layer_count)
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
) -> ExportArtifacts:
    """Render once and create both reports before the caller publishes the ZIP."""
    from .html_report import OUTPUT_FILENAME as HTML_FILENAME, write_html_report
    from .render import SectionRenderer
    from .workbook import OUTPUT_FILENAME, write_workbook

    validate_current(plan, plan.config, plan.snapshot)
    scratch_dir = Path(scratch_dir)
    if not scratch_dir.is_dir():
        raise ValidationError("The impedance export scratch directory does not exist.")
    renderer = (renderer_factory or SectionRenderer)(board, pcbnew_module)
    images = []
    total_bytes = 0
    try:
        for index, section in enumerate(plan.sections, 1):
            destination = scratch_dir / f"impedance-section-{index:04d}.png"
            result = Path(
                renderer.render(
                    section, destination, width_px=IMAGE_WIDTH, height_px=IMAGE_HEIGHT
                )
            )
            if result.resolve() != destination.resolve():
                raise ValidationError(
                    "The impedance renderer did not create its expected PNG."
                )
            capture = CapturedImage.load(destination)
            total_bytes += len(capture.data)
            if total_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise ValidationError(
                    "Position images exceed the 128 MiB total size limit."
                )
            images.append(capture)
    finally:
        close = getattr(renderer, "close", None)
        if callable(close):
            close()
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
            ),
            html_report,
        ),
    )
    for result, expected in outputs:
        if Path(result).resolve() != expected.resolve() or not expected.is_file():
            raise ValidationError(
                "The impedance writer did not create its expected report."
            )
    return ExportArtifacts(workbook, html_report)
