"""Write an offline HTML companion from the workbook's reviewed rows and PNGs."""

from base64 import b64encode
from collections.abc import Sequence
from decimal import Decimal
from html import escape
import os
from pathlib import Path
import tempfile
from typing import Optional

from .review_tracking import (
    ReviewTracking,
    find_spacing_image,
    format_timestamp,
    spacing_capture_fingerprint,
    spacing_image_current,
)
from .service import ReportRow, validate_report_rows, validate_spacing_review_images
from .spacing_checks import SpacingCheck, spacing_check_details, spacing_check_label
from .spacing_review import SpacingReviewImage
from .stackup_model import Stackup, validate_stackup
from .width_checks import WidthCheck, format_width_delta_nm, format_width_nm

OUTPUT_FILENAME = "Required_impedance_control.html"
_KINDS = {
    "single_ended": "Single-ended · Non-coplanar",
    "single_ended_coplanar": "Single-ended · CPWG",
    "differential": "Differential pair · Non-coplanar",
    "differential_coplanar": "Differential pair · CPWG",
}
_STYLE = """
:root { color-scheme: light; font: 16px/1.5 system-ui, sans-serif; color: #192b3b;
  background: #eef2f5; }
* { box-sizing: border-box; }
body { margin: 0; }
header { background: #162b3a; color: #fff; border-bottom: 5px solid #ffff00;
  padding: 2rem max(1rem, calc((100vw - 1320px) / 2)); }
h1 { margin: .25rem 0 .6rem; font-size: clamp(1.6rem, 3vw, 2.5rem); }
header p { margin: .4rem 0; max-width: 85ch; }
.eyebrow { font-size: .85rem; letter-spacing: .08em; text-transform: uppercase;
  color: #d7e4ec; }
main { max-width: 1360px; margin: auto; padding: 1.25rem; }
.stackup { background: #fff; padding: 1.25rem; border-radius: 10px;
  border: 1px solid #d6e0e6; margin-bottom: 1.25rem; }
.stackup dl { padding: .5rem 0; display: grid;
  grid-template-columns: minmax(160px, 1fr) 3fr; gap: .35rem 1rem; }
.stackup dt, .stackup dd { margin: 0; }
.stackup p { color: #546c7d; font-size: .9rem; margin-bottom: 0; }
.preferred { display: inline-flex; gap: .4rem; align-items: center;
  background: #fff1d7; color: #784215; padding: .25rem .65rem;
  border-radius: 6px; font-weight: 600; margin: .7rem 0; }
.preferred svg { width: 1.1rem; height: 1.1rem; }
details { margin-top: .8rem; overflow-x: auto; }
.assumptions { padding-left: 1.1rem; margin: .2rem 0; font-size: .85rem; }
summary { cursor: pointer; font-weight: 600; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; margin-top: .6rem; }
th, td { padding: .45rem .6rem; text-align: left; border-bottom: 1px solid #e0e7eb; }
.notice { padding: .9rem 1.1rem; background: #fff; border-left: 4px solid #476f89;
  margin-bottom: 1.25rem; }
article { background: #fff; border: 1px solid #d6e0e6; border-radius: 10px;
  overflow: hidden; margin: 0 0 1.5rem; break-inside: avoid; }
.row-heading { padding: 1rem 1.25rem; border-bottom: 1px solid #e0e7eb;
  display: flex; align-items: baseline; gap: .8rem; flex-wrap: wrap; }
h2 { font-size: 1.2rem; margin: 0; }
.row-id { color: #425e72; font-weight: 600; text-decoration: none; }
.sheet-row { font-size: .85rem; color: #546c7d; margin-left: auto; }
.row-content { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(0, 3fr); }
dl { padding: 1.1rem 1.25rem; margin: 0; }
dt { color: #546c7d; font-size: .8rem; margin-top: .75rem; }
dt:first-child { margin-top: 0; }
dd { margin: .1rem 0 0; font-weight: 500; }
dd, h1, h2 { overflow-wrap: anywhere; }
.target { font-size: 1.5rem; font-weight: 700; }
figure { margin: 0; padding: 1rem; align-self: center; min-width: 0; }
img { display: block; width: 100%; height: auto; border-radius: 5px; }
.pcb-image { position: relative; }
.spacing-review .row-content { grid-template-columns: minmax(180px, 1fr) minmax(0, 4fr); }
.spacing-capture { overflow: auto; max-height: 85vh; margin-top: .5rem; }
.spacing-full-size:checked ~ .spacing-capture img { width: auto; max-width: none; }
.spacing-original { margin-top: 1rem; }
figcaption { color: #546c7d; font-size: .8rem; margin-top: .5rem; }
footer { color: #546c7d; padding: .5rem 1rem 2rem; text-align: center; }
@media (max-width: 760px) {
  main { padding: .75rem; }
  .row-content { grid-template-columns: 1fr; }
  dl { display: grid; grid-template-columns: 1fr 2fr; gap: .4rem .8rem; }
  dt, dt:first-child, dd { margin: 0; }
  .sheet-row { margin-left: 0; }
}
@media print {
  :root { background: #fff; font-size: 11px; }
  header { color: #192b3b; background: #fff; padding: 1rem; }
  .eyebrow { color: #425e72; }
  main { padding: 0; }
  article { border-radius: 0; }
}
"""


def _text(value: str, field: str) -> str:
    """Escape names for HTML, displaying unsupported codepoints as literal notation."""
    if not isinstance(value, str):
        raise TypeError(f"HTML report {field} must be text.")
    # Extra display metadata must not add a new length restriction to names
    # already accepted by the model and saved review. Make characters that
    # cannot safely be serialized visible without altering persisted names.
    display = "".join(
        f"\\u{ord(character):04x}"
        if (ord(character) < 32 and character not in "\t\n\r")
        or 0xD800 <= ord(character) <= 0xDFFF
        else character
        for character in value
    )
    return escape(display, quote=True)


def _field(label: str, value: str, *, css_class: str = "") -> str:
    """Format one escaped label/value pair; only callers supply the CSS class."""
    return f'<dt>{escape(label)}</dt><dd class="{css_class}">{_text(value, label)}</dd>'


def _review_timestamp(timestamp: str, current: bool, *, approval: bool = False) -> str:
    """Distinguish saved historical review actions from the currently exported row."""
    if not timestamp:
        return "Not recorded"
    if approval:
        status = (
            "Current layer settings and captures"
            if current
            else "Previous layer settings or captures"
        )
    else:
        status = "Current capture" if current else "Previous capture"
    return f"{format_timestamp(timestamp, local=False)} · {status}"


def _width_fields(check: Optional[WidthCheck]) -> list[str]:
    """Explain nominal dimensions and stale evidence without an impedance verdict."""
    if check is None:
        return [_field("Nominal width comparison", "Not calculated")]
    fields = [_field("Nominal width comparison", check.message)]
    if check.target_width_nm is not None:
        fields.append(
            _field(
                "Calculated nominal width"
                if check.result_current
                else "Previous nominal width",
                format_width_nm(check.target_width_nm),
            )
        )
    if check.delta_nm is not None:
        fields.append(
            _field("Actual − nominal width", format_width_delta_nm(check.delta_nm))
        )
    if check.provider:
        state = "Current inputs" if check.result_current else "Historical inputs"
        fields.extend(
            (
                _field("Calculation result", f"{state} · {check.result_status}"),
                _field("Calculation provider", check.provider),
                _field(
                    "Calculated at",
                    format_timestamp(check.calculated_at_utc, local=False)
                    if check.calculated_at_utc
                    else "Not recorded",
                ),
            )
        )
        fields.append(_field("Saved calculation model", check.model or "Not recorded"))
        assumptions = (
            '<ul class="assumptions">'
            + "".join(
                f"<li>{_text(item, 'assumption')}</li>" for item in check.assumptions
            )
            + "</ul>"
            if check.assumptions
            else "Not recorded in the saved calculation"
        )
        fields.append(f"<dt>Saved model assumptions</dt><dd>{assumptions}</dd>")
    return fields


def _spacing_fields(check: Optional[SpacingCheck]) -> list[str]:
    """Render shared advisory spacing evidence without remeasuring geometry."""
    if check is None:
        return [_field("Pair spacing", "Unverified · No spacing assessment")]
    fields = [_field("Pair spacing", spacing_check_label(check))]
    details = spacing_check_details(check)
    fields.append(
        "<dt>Spacing assessment</dt><dd>"
        + "<br>".join(
            _text(line, "spacing assessment") for line in details.splitlines()
        )
        + "</dd>"
    )
    fields.append(
        _field(
            "Spacing check scope",
            "Advisory routed trace-to-trace dimensional check; recognized local "
            "transitions are excluded. Coplanar ground gaps, "
            "reference-plane continuity and achieved impedance are not verified.",
        )
    )
    return fields


def _stackup_summary(stackup: Optional[Stackup]) -> str:
    """Show the selected frozen construction, with preference and charge separate."""
    if stackup is None:
        return (
            '<section class="stackup"><h2>Stackup not selected</h2>'
            "<p>Nominal widths are not calculated. The report retains the actual routed "
            "widths and the declared targets and reference layers.</p></section>\n"
        )
    validate_stackup(stackup)
    inner_copper = (
        f"{stackup.inner_copper_oz} oz"
        if stackup.inner_copper_oz
        else "Not applicable"
        if stackup.layer_count == 2
        else "Not specified"
    )
    charge = {
        "additional": "Yes",
        "none": "No",
        "unknown": "Unknown — confirm when ordering",
    }[stackup.charge_status]
    fields = [
        _field("Stackup ID", stackup.stackup_id),
        _field("Copper layers", str(stackup.layer_count)),
        _field("Board thickness", f"{stackup.thickness_mm} mm"),
        _field("Outer copper", f"{stackup.outer_copper_oz} oz"),
        _field("Inner copper", inner_copper),
        _field("Additional charge", charge),
        _field("Source", stackup.source_url),
        _field(
            "Source retrieved at",
            format_timestamp(stackup.retrieved_at_utc, local=False)
            if stackup.retrieved_at_utc
            else "Not recorded",
        ),
    ]
    if not stackup.calculator_id:
        preference = "<p>Preferred status unavailable; calculator unavailable.</p>"
    elif stackup.preferred:
        icon = (Path(__file__).parent / "resources" / "preferred-fire.svg").read_text(
            encoding="utf-8"
        )
        preference = f'<span class="preferred">{icon}<span>Preferred</span></span>'
    else:
        preference = "<p>Not marked Preferred in the selected catalog record.</p>"
    construction = ""
    if stackup.layers:
        rows = []
        for layer in stackup.layers:
            cells = (
                layer.name,
                layer.kind,
                f"{layer.thickness_mm} mm",
                layer.material or "Not specified",
                layer.dielectric_constant or "Not specified",
            )
            rows.append(
                "<tr>"
                + "".join(f"<td>{_text(value, 'construction')}</td>" for value in cells)
                + "</tr>"
            )
        construction = (
            "<details><summary>Frozen construction, top to bottom</summary><table>"
            "<thead><tr><th>Layer</th><th>Type</th><th>Thickness</th><th>Material</th>"
            "<th>Dielectric constant</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></details>"
        )
    return (
        f'<section class="stackup"><h2>Selected stackup: {_text(stackup.name, "stackup name")}</h2>'
        f"{preference}<dl>{''.join(fields)}</dl>{construction}"
        "<p>This is the frozen construction selected for this board. A catalog refresh "
        "does not replace it. Preferred status does not determine additional charges.</p>"
        "</section>\n"
    )


def _card(row: ReportRow, index: int) -> str:
    """Embed one original PNG without recoloring, re-rendering, or resampling."""
    values = row
    signal = f"{row.signal_layer} ({row.physical_signal_layer})"
    references = ", ".join(
        f"{canonical} ({physical})"
        for canonical, physical in zip(
            row.reference_layers, row.physical_reference_layers
        )
    )
    fields = [
        _field(
            "Target impedance",
            f"{format(Decimal(values.target_ohms), 'f')} Ω",
            css_class="target",
        ),
        _field("Construction", _KINDS[values.kind]),
        _field("Nets", ", ".join(row.nets) or "Unnamed net"),
        _field("Net class", row.net_class or "Not recorded"),
        _field("Signal copper layer", signal),
        _field("Ground reference layers", references),
        _field("Actual trace width", format_width_nm(values.width_nm)),
    ]
    if values.kind.startswith("differential") and values.spacing_nm is not None:
        fields.append(
            _field("Declared pair spacing", format_width_nm(values.spacing_nm))
        )
        fields.extend(_spacing_fields(row.spacing_check))
    if values.kind.endswith("_coplanar") and values.ground_gap_nm is not None:
        fields.append(
            _field("Coplanar ground gap", format_width_nm(values.ground_gap_nm))
        )
    fields.extend(_width_fields(row.width_check))
    fields.extend(
        (
            _field(
                "Last viewed",
                _review_timestamp(row.last_viewed_at_utc, row.last_viewed_current),
            ),
            _field(
                "Layer approved at",
                _review_timestamp(
                    row.layer_approved_at_utc,
                    row.layer_approval_current,
                    approval=True,
                ),
            ),
        )
    )
    width, height = row.image.width, row.image.height
    encoded = b64encode(row.image.data).decode("ascii")
    alt = _text(
        f"Reviewed PCB section {index}: {', '.join(row.nets)} on {signal}",
        "image description",
    )
    return (
        f'<article id="row-{index}"><div class="row-heading">'
        f'<a class="row-id" href="#row-{index}">Row {index}</a>'
        f"<h2>{_text(row.specification, 'specification')}</h2>"
        f'<span class="sheet-row">XLSX sheet row {index + 1}</span></div>'
        f'<div class="row-content"><dl>{"".join(fields)}</dl><figure>'
        f'<div class="pcb-image"><img src="data:image/png;base64,{encoded}" width="{width}" '
        f'height="{height}" alt="{alt}"></div>'
        "<figcaption>The yellow outline identifies this row's route or differential pair. "
        "This is the same capture embedded in the workbook.</figcaption>"
        "</figure></div></article>\n"
    )


def _spacing_card(
    image: SpacingReviewImage,
    index: int,
    tracking: Optional[ReviewTracking],
) -> str:
    """Show a native close-up with external labels and exact approval evidence."""
    entry, capture = image.entry, image.capture
    record = (
        find_spacing_image(tracking, entry.spec_id, entry.layer, entry.view_id)
        if tracking is not None
        else None
    )
    current = (
        record is not None
        and record.issue_id == entry.issue_id
        and spacing_image_current(
            record,
            entry.input_digest,
            spacing_capture_fingerprint(entry.input_digest, capture.sha256),
            approved=True,
        )
    )
    status = (
        f"Approved · {format_timestamp(record.approved_at_utc, local=False)}"
        if current and record is not None and record.approved_at_utc
        else "Not approved for this capture"
    )
    fields = (
        _field("Nets", ", ".join(entry.pair)),
        _field("Signal copper layer", entry.layer),
        _field("Spacing concern", entry.caption),
        _field("Image approval", status),
    )
    encoded = b64encode(capture.data).decode("ascii")
    alt = _text(
        f"Spacing review {index}: {entry.caption} on {entry.layer}",
        "spacing image description",
    )
    original = ""
    if capture.original is not None:
        comparison = capture.original
        original_encoded = b64encode(comparison.data).decode("ascii")
        original = (
            '<details class="spacing-original"><summary>Original image — comparison only</summary>'
            f'<img src="data:image/png;base64,{original_encoded}" width="{comparison.width}" '
            f'height="{comparison.height}" alt="Original native detail without spacing annotations">'
            "<p>Viewing this comparison does not approve the combined spacing image.</p></details>"
        )
    return (
        f'<article class="spacing-review" id="spacing-review-{index}">'
        '<div class="row-heading">'
        f'<a class="row-id" href="#spacing-review-{index}">Spacing image {index}</a>'
        f"<h2>{_text(entry.caption, 'spacing caption')}</h2></div>"
        f'<div class="row-content"><dl>{"".join(fields)}</dl><figure>'
        f'<input class="spacing-full-size" type="checkbox" id="spacing-size-{index}"> '
        f'<label for="spacing-size-{index}">Full-size image</label><div class="spacing-capture">'
        f'<img src="data:image/png;base64,{encoded}" width="{capture.width}" '
        f'height="{capture.height}" alt="{alt}"></div>'
        "<figcaption>Independent combined spacing image, separate from the workbook. "
        "Approval acknowledges this image; it does not change the measured verdict "
        f"or declared manufacturing spacing.</figcaption>{original}</figure></div></article>\n"
    )


def write_html_report(
    rows: Sequence[ReportRow],
    destination: Path,
    *,
    board_name: str = "PCB",
    stackup: Optional[Stackup] = None,
    spacing_reviews: Sequence[SpacingReviewImage] = (),
    review_tracking: Optional[ReviewTracking] = None,
) -> Path:
    """Atomically publish a self-contained HTML report; preserve old output on failure.

    Reuse the workbook's exact captured rows without reopening or decoding PNGs.
    Images are streamed one at a time to the temporary HTML file; its destination's
    parent must exist. No browser, JavaScript, network, or extra assets are needed.
    Base64 expands the bounded 128 MiB image payload to at most about 171 MiB.
    Display names retain the model's existing length policy and are HTML-escaped.
    """
    validate_report_rows(rows)
    validate_spacing_review_images(
        spacing_reviews, manufacturing_bytes=sum(len(row.image.data) for row in rows)
    )
    title = _text(board_name, "board name")
    stackup_summary = _stackup_summary(stackup)
    icon_attribution = (
        '<p>Preferred icon: <a href="https://github.com/FortAwesome/Font-Awesome/blob/6.7.2/svgs/solid/fire.svg">'
        "Font Awesome Free 6.7.2 Fire</a>, © 2024 Fonticons, Inc., "
        '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. '
        "Adapted with orange fill and display dimensions; original path unchanged. "
        "No endorsement is implied.</p>"
        if stackup is not None and stackup.calculator_id and stackup.preferred
        else ""
    )
    destination = Path(destination)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(
                '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                "img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
                f"<title>{title} — Impedance review</title><style>{_STYLE}</style></head><body>"
                '<header><p class="eyebrow">Controlled impedance · Offline review companion</p>'
                f"<h1>{title}</h1><p>{len(rows)} reviewed sections · Same order and images as "
                "Required_impedance_control.xlsx</p></header><main>"
                f"{stackup_summary}"
                '<p class="notice">Impedance targets and geometry describe designer intent, '
                "not field-solver verification or fabrication approval. Ground references are "
                "the reviewed layer selections. Confirm the stackup and achieved impedance "
                "with your manufacturer. Nominal widths use the selected stackup and declared "
                "reference layers and pair/ground gaps; an exact width match is not impedance "
                "verification. No solver or manufacturing tolerance is used for the width comparison. "
                "The separate routed pair-spacing check is advisory and uses an inclusive "
                "±0.001 mm dimensional band with measurement uncertainty reported separately. "
                "Incomplete coverage cannot establish a match. "
                "Review times are recorded in UTC; exporting a report "
                "does not mark captures as viewed or layers as approved.</p>\n"
            )
            if spacing_reviews:
                output.write(
                    '<section aria-label="Separate spacing review images">'
                    "<h2>Spacing review images</h2>"
                    "<p>Each close-up is reviewed individually and excluded from Excel.</p>\n"
                )
                for index, image in enumerate(spacing_reviews, start=1):
                    output.write(_spacing_card(image, index, review_tracking))
                output.write("</section>\n")
            for index, row in enumerate(rows, start=1):
                output.write(_card(row, index))
            output.write(
                "</main><footer>Self-contained report · All images are embedded; "
                f"no internet connection is required.{icon_attribution}</footer></body></html>\n"
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination
