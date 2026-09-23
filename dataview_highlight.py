"""Reusable text-highlight helpers and DataView renderer."""

from __future__ import annotations

from collections.abc import Callable
import re

try:
    import wx  # pylint: disable=import-error
    import wx.dataview as dv  # pylint: disable=import-error
except ImportError:  # pragma: no cover - test environments may not have wx
    wx = None  # type: ignore[assignment]
    dv = None  # type: ignore[assignment]


_HIGHLIGHT_FG = (180, 120, 0)
_HIGHLIGHT_FG_SELECTED = (255, 215, 64)
_MIN_HIGHLIGHT_TERM_LENGTH = 2
_HIGHLIGHT_VALUE_SEPARATOR = "\x1f"

# Package families the catalog actually uses, as
#
#     SELECT upper(<leading letters>), sum(count) FROM parts GROUP BY Package
#
# reports them over a 717,025-part snapshot, keeping every family carrying at
# least 200 parts.  Mounting styles the catalog puts in the same column -- SMD,
# Plugin, Push-Pull, Case, ITO -- are deliberately absent: they describe how a
# part attaches rather than which package it is, and they match right across
# the catalog.  This is vocabulary, not a list of supported footprints; a
# family here still has to appear in the footprint name to be used.
_PACKAGE_FAMILIES = frozenset(
    """
    ABS BGA BOLT CDIP DDPAK DFN DIP DO DPAK DSBGA DSO DT ESOP FBGA GBU HC HTQFP
    HTSSOP HVQFN LFCSP LFPAK LGA LL LQFP MELF MINIMELF MSOP NFBGA PDFN PDIP PLCC
    POWERDI POWERPAK QFN QSOP SC SIP SMA SMAF SMAG SMB SMBF SMC SMCG SO SOD SOIC
    SON SOP SOT SSOP TDFN TDSON TFBGA TO TOLL TQFN TQFP TSOP TSOT TSSOP UDFN
    UFBGA UFQFPN UQFN VFQFPN VQFN VSON VSSOP WDFN WLCSP WQFN WSON
    """.split()
)

_FAMILY_ALTERNATION = "|".join(sorted(_PACKAGE_FAMILIES, key=len, reverse=True))

# A whole segment that is a family, an optional lettered subtype, then size and
# pin-count groups: SOIC-8, SOT-23-5, QFN-24-1EP, HC49, SOD-323F, DFN-S-8-1EP.
# Anchored at both ends, and bare letters may only follow a number or a dash, so
# a segment that merely begins with a family word cannot match: Small is not an
# SMA, and Fastron's SMCC series is not an SMC.
#
# The subtype is matched so the segment is recognised, then dropped: the catalog
# writes DFN-8 and LFCSP-24, never DFN-S-8 or LFCSP-VQ-24.  It only counts as a
# subtype when a dashed number follows, or the letters and the digits beside
# them would be read as a package that was never there: D_MELF-RM10 is a MELF
# diode, not a MELF10, and Texas_VQFN-RNR0011A-11 is not a VQFN0011.
#
# Only the first group may omit its dash, as HC49 does.  Making the separator
# mandatory after that leaves a run of digits exactly one way to divide, which
# matters because this runs once per row: with the dash optional throughout, a
# 24-digit segment that fails the anchor took a second to reject.
_PACKAGE_SEGMENT_RE = re.compile(
    r"^(?P<family>" + _FAMILY_ALTERNATION + r")"
    r"(?:-[A-Za-z]{1,3}(?=-\d))?(?P<groups>(?:\d+[A-Za-z]*)?(?:-\d+[A-Za-z]*)*)$",
    re.IGNORECASE,
)

# One size or pin-count group within that tail: -23, -5, -1EP, 323F.
_DESIGNATOR_GROUP_RE = re.compile(r"(-?)(\d+)([A-Za-z]*)")

# Chip sizes: R_0603_1608Metric, C_01005_0402Metric.  The imperial code is what
# the catalog writes, and KiCad's library always pairs it with its metric
# companion, so the pair is what identifies it -- four digits elsewhere in a
# name are a dimension or a part number, as in BatteryHolder_Keystone_1060.
_CHIP_SIZE_RE = re.compile(r"_(\d{4,5})_\d+Metric", re.IGNORECASE)

# The same code on its own, as KiCad 4 libraries spelled it -- R_0603,
# C_0603_HandSoldering -- and as boards built from them still do, since an
# upgrade keeps the footprint ids it finds.  The code must be a whole segment
# introduced by one of the classes the library pairs with a chip size, and
# nothing else: any other word ahead of four digits (BatteryHolder, Crystal,
# Jack) is naming something else, and a wrong token here empties the search.
_BARE_CHIP_SIZE_RE = re.compile(
    r"^(?:R|C|L|D|LED|Fuse)_(\d{4,5})(?:_|$)", re.IGNORECASE
)

# SMD electrolytics: KiCad names them by diameter and height, CP_Elec_6.3x5.9,
# and the catalog by diameter and length, SMD,D6.3xL5.9mm.  Only the diameter
# is worth matching: boards routinely take a 7.7mm-tall can for a 5.9mm
# footprint, so pinning the height finds a fraction of the real candidates.
# The trailing x anchors the diameter, keeping D6.3 off D6.35.
_ELECTROLYTIC_RE = re.compile(r"^CP_Elec_(\d+(?:\.\d+)?)x\d", re.IGNORECASE)

# Tantalum case codes: CP_EIA-3528-21_Kemet-B, which the catalog writes
# CASE-B-3528-21(mm).  As with the electrolytics above, only the land size is
# worth matching -- the trailing number is KiCad's height, and pinning it finds
# a sliver or nothing: 7343-40 reaches 2 rows where 7343- reaches 692, and
# 3528-15 reaches none where 3528- reaches 811.  The dash is kept because the
# bare size is also a chip package: 1608- is 377 rows, 1608 is 3,724.
_EIA_CASE_RE = re.compile(r"^CP_EIA-(\d+)-\d", re.IGNORECASE)

# Below three characters Library.search falls back to a LIKE over the
# description, where a package designator is prose rather than a package: "SC"
# matches every part whose description happens to contain it.
_MIN_TOKEN_LENGTH = 3

# SIOC-8 was a transposition of SOIC-8: no KiCad footprint and no catalog part
# spells it, so this entry never fired.  These are highlight terms only -- the
# search ANDs its keywords, so no alias here can express "SOIC-8 or SO-8".
_FOOTPRINT_ALIAS_FORWARD = {
    "SOIC-8": "SO-8",
    "SOT-23": "TO-236",
}
_FOOTPRINT_ALIAS_MAP = dict(_FOOTPRINT_ALIAS_FORWARD)
_FOOTPRINT_ALIAS_MAP.update(
    {target: source for source, target in _FOOTPRINT_ALIAS_FORWARD.items()}
)


def normalize_highlight_terms(query: str) -> list[str]:
    """Split keyword query into normalized terms suitable for highlighting."""
    terms = []
    for raw_term in query.split():
        term = raw_term.strip().strip("%")
        if term:
            lowered = term.casefold()
            if lowered not in terms:
                terms.append(lowered)
    return terms


def find_highlight_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """Return merged `(start, end)` spans for all term matches in `text`."""
    if not text or not terms:
        return []

    lowered_text = text.casefold()
    spans: list[tuple[int, int]] = []

    for term in terms:
        start = 0
        while True:
            match_start = lowered_text.find(term, start)
            if match_start == -1:
                break
            match_end = match_start + len(term)
            spans.append((match_start, match_end))
            start = match_end

    if not spans:
        return []

    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def filtered_highlight_terms(query: str) -> list[str]:
    """Return normalized terms that are long enough to highlight."""
    return [
        term
        for term in normalize_highlight_terms(query)
        if len(term) >= _MIN_HIGHLIGHT_TERM_LENGTH
    ]


def encode_highlighted_value(text: str, terms: list[str]) -> str:
    """Pack display text and row-specific highlight terms into a single string."""
    packed_terms = []
    for term in terms:
        if term is None:
            continue
        cleaned = str(term).strip().strip("%")
        if cleaned:
            packed_terms.append(cleaned)

    return _HIGHLIGHT_VALUE_SEPARATOR.join(
        ["" if text is None else str(text), *packed_terms]
    )


def decode_highlighted_value(value: str) -> tuple[str, list[str]]:
    """Unpack display text and normalized highlight terms from a packed string."""
    text = "" if value is None else str(value)
    if _HIGHLIGHT_VALUE_SEPARATOR not in text:
        return text, []

    parts = text.split(_HIGHLIGHT_VALUE_SEPARATOR)
    display_text = parts[0]
    raw_terms = " ".join(parts[1:])
    return display_text, normalize_highlight_terms(raw_terms)


def expand_value(reference: str, value: str) -> list[str]:
    """Return value variants used for highlight matching.

    For resistor references (`R*`), include ohm-symbol equivalents so terms like
    `390R` can also match `390Ω`, and `10K` can also match `10KΩ`.
    For capacitor references (`C*`), include `u`/`µ` interchangeable variants
    and optional `F`-suffixed forms.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return []

    variants = [raw]
    ref = "" if reference is None else str(reference).strip()
    upper_ref = ref.upper()

    if upper_ref.startswith("R"):
        if raw.endswith("Ω"):
            base = raw[:-1]
            if base and base[-1] in "RrOoKkMm":
                variants.append(base)
        else:
            last = raw[-1]
            if last in "RrOo":
                variants.append(f"{raw[:-1]}Ω")
            elif last in "KkMm":
                variants.append(f"{raw}Ω")

    if upper_ref.startswith("C"):
        has_micro = "µ" in raw or "u" in raw or "U" in raw
        if has_micro:
            swapped = raw.replace("µ", "u") if "µ" in raw else re.sub(r"[uU]", "µ", raw)

            if raw.endswith("F"):
                variants.extend([raw, swapped, raw[:-1], swapped[:-1]])
            else:
                variants.extend([raw, swapped, f"{raw}F", f"{swapped}F"])

    deduped = []
    for variant in variants:
        if variant not in deduped:
            deduped.append(variant)
    return deduped


def _base_designator(segment: str) -> str:
    """Return the package a whole footprint-name segment names, or "" if none.

    The designator is the family and its numbers: SOT-23-5 keeps its pin count,
    because the catalog spells that and it reaches 7,302 rows where SOT-23
    reaches 31,568.  KiCad's letter suffixes are dropped, because the catalog
    does not use them -- it writes SOD-323 for SOD-323F and has no SOIC-8-1EP
    at all.
    """
    match = _PACKAGE_SEGMENT_RE.match(segment)
    if match is None:
        return ""
    designator = match.group("family")
    for position, (dash, digits, letters) in enumerate(
        _DESIGNATOR_GROUP_RE.findall(match.group("groups"))
    ):
        if position and letters:
            # A later group carrying letters qualifies the package rather than
            # sizing it: the EP of QFN-24-1EP counts exposed pads.
            break
        designator += dash + digits
        if letters:
            # Letters on the size itself are KiCad's: SOD-323F is the catalog's
            # SOD-323, which also reaches the SOD-323F and SOD-323FL rows.
            break
    return designator


def simplify_footprint_name(footprint: str) -> str:
    """Return the catalog's package for a footprint name, or "" if it has none.

    ``Package_SO:SOIC-8_3.9x4.9mm_P1.27mm`` gives ``SOIC-8``,
    ``Resistor_SMD:R_0603_1608Metric`` gives ``0603``, and
    ``Connector_JST:JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical`` gives ``""``.

    Returning nothing is a real answer, not a failure.  Most KiCad footprints
    are connectors, mounting holes and test points, which the catalog either
    does not stock or names in a way no rule can reach from the footprint; for
    those the value alone is the better search.  The part selector ANDs this
    token with the value, so a token the catalog never writes returns nothing
    at all rather than merely returning too much.
    """
    if not footprint:
        return ""
    name = str(footprint).split(":")[-1]

    chip = _CHIP_SIZE_RE.search(name) or _BARE_CHIP_SIZE_RE.match(name)
    if chip:
        return chip.group(1)
    electrolytic = _ELECTROLYTIC_RE.match(name)
    if electrolytic:
        return f"SMD,D{electrolytic.group(1)}x"
    eia = _EIA_CASE_RE.match(name)
    if eia:
        return f"{eia.group(1)}-"

    segments = name.split("_")
    for index, segment in enumerate(segments):
        designator = _base_designator(segment)
        if len(designator) < _MIN_TOKEN_LENGTH:
            continue
        if not any(c.isdigit() for c in designator) and len(segments[index + 1 :]) > 1:
            # A family carrying no size of its own needs the rest of the name
            # to vouch for it.  One trailing segment is a variant of that
            # package -- D_SMA_Handsoldering, R_MELF_MMB-0207, D_SMB_Modified.
            # More than one means the family word is describing something else:
            # SW_DIP_SPSTx01_Slide_9.78x4.72mm is a DIP switch, which the
            # catalog lists under Plugin,P=2.54mm, and
            # SMA_Amphenol_132134-10_Vertical is an RF connector.
            continue
        return designator
    return ""


def expand_footprint(reference: str, footprint: str) -> list[str]:
    """Return footprint variants used for highlight matching.

    The package designator the catalog would name, plus the alias spellings
    known to be equivalent to it.
    """
    raw = "" if footprint is None else str(footprint).strip()
    if not raw:
        return []

    footprint_name = raw.split(":")[-1]
    upper_name = footprint_name.upper()
    variants: list[str] = []

    simplified = simplify_footprint_name(footprint_name)
    if simplified:
        variants.append(simplified)

    # Common package aliases used by parts databases and footprints.
    for source, target in _FOOTPRINT_ALIAS_MAP.items():
        if source in upper_name:
            variants.append(target)

    deduped = []
    for variant in variants:
        if variant and variant not in deduped:
            deduped.append(variant)
    return deduped


class HighlightQueryCache:
    """Cache normalized query terms and highlight spans for one active query."""

    def __init__(self):
        self._query = ""
        self._terms: list[str] = []
        self._span_cache: dict[str, list[tuple[int, int]]] = {}

    def prepare(self, query: str):
        """Prepare cache state for a query, resetting cached spans on change."""
        if query != self._query:
            self._query = query
            self._terms = filtered_highlight_terms(query)
            self._span_cache.clear()

    def clear(self):
        """Clear all cached query and span data."""
        self._query = ""
        self._terms = []
        self._span_cache.clear()

    def get_terms(self) -> list[str]:
        """Return terms prepared for the active query."""
        return self._terms

    def get_spans(self, text: str) -> list[tuple[int, int]]:
        """Return cached spans for text, computing and storing on cache miss."""
        spans = self._span_cache.get(text)
        if spans is None:
            spans = find_highlight_spans(text, self._terms)
            self._span_cache[text] = spans
        return spans


if wx is not None and dv is not None:  # pragma: no branch

    class HighlightedTextRenderer(dv.DataViewCustomRenderer):
        """Simple text renderer that highlights keyword matches."""

        def __init__(
            self,
            highlight_text_getter: Callable[[], str] | None = None,
            align: int = wx.ALIGN_LEFT,
            value_decoder: Callable[[str], tuple[str, list[str]]] | None = None,
        ):
            super().__init__("string", dv.DATAVIEW_CELL_INERT, align)
            self._highlight_text_getter = highlight_text_getter
            self._value_decoder = value_decoder
            self._value = ""
            self._query_cache = HighlightQueryCache()

        def SetValue(self, value: str) -> bool:
            """Store value to render for the current cell."""
            self._value = "" if value is None else str(value)
            return True

        def GetValue(self) -> str:
            """Return current cell value."""
            return self._value

        def _resolve_text_and_terms(self) -> tuple[str, list[str]]:
            """Resolve display text and normalized highlight terms."""
            if self._value_decoder is not None:
                return self._value_decoder(self._value)

            highlight_text = (
                self._highlight_text_getter() if self._highlight_text_getter else ""
            )
            self._query_cache.prepare(highlight_text)
            return self._value, self._query_cache.get_terms()

        def GetSize(self):
            """Return a best-effort size for the current text."""
            owner = self.GetOwner()
            font = (
                owner.GetOwner().GetFont()
                if owner is not None and owner.GetOwner() is not None
                else wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
            )
            display_text, _ = self._resolve_text_and_terms()
            dc = wx.ScreenDC()
            dc.SetFont(font)
            width, height = dc.GetTextExtent(display_text or "Hg")
            return wx.Size(width + 8, height + 6)

        def Render(self, rect, dc, state):
            """Draw the cell text and highlight search-term matches."""
            selected = bool(state & dv.DATAVIEW_CELL_SELECTED)
            foreground = wx.SystemSettings.GetColour(
                wx.SYS_COLOUR_HIGHLIGHTTEXT if selected else wx.SYS_COLOUR_LISTBOXTEXT
            )
            highlight = wx.Colour(
                *(_HIGHLIGHT_FG_SELECTED if selected else _HIGHLIGHT_FG)
            )

            dc.SetTextForeground(foreground)
            dc.SetBackgroundMode(wx.TRANSPARENT)

            text, terms = self._resolve_text_and_terms()
            if not text:
                return True

            if self._value_decoder is None and not terms:
                text_height = dc.GetTextExtent("Hg")[1]
                x = rect.x + 4
                y = rect.y + max(0, (rect.height - text_height) // 2)

                dc.SetClippingRegion(rect)
                try:
                    dc.DrawText(text, x, y)
                finally:
                    dc.DestroyClippingRegion()
                return True

            spans = (
                self._query_cache.get_spans(text)
                if self._value_decoder is None
                else find_highlight_spans(text, terms)
            )
            text_height = dc.GetTextExtent("Hg")[1]
            x = rect.x + 4
            y = rect.y + max(0, (rect.height - text_height) // 2)

            dc.SetClippingRegion(rect)
            try:
                cursor = 0
                for start, end in spans:
                    if start > cursor:
                        segment = text[cursor:start]
                        dc.SetTextForeground(foreground)
                        dc.DrawText(segment, x, y)
                        x += dc.GetTextExtent(segment)[0]

                    segment = text[start:end]
                    segment_width, _ = dc.GetTextExtent(segment)
                    dc.SetTextForeground(highlight)
                    dc.DrawText(segment, x, y)
                    x += segment_width
                    cursor = end

                if cursor < len(text):
                    dc.SetTextForeground(foreground)
                    dc.DrawText(text[cursor:], x, y)
            finally:
                dc.DestroyClippingRegion()
            return True
